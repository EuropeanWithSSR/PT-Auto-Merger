import os
import sys
import fcntl
import requests
import time
import logging
from logging.handlers import RotatingFileHandler

# ================= 基础配置区 =================
QB_URL = "http://127.0.0.1:8080"
USERNAME = "admin"
PASSWORD = "adminadmin"

# ================= 阈值系统配置 =================
GAP_THRESHOLD_BYTES = 5 * 1024 * 1024 * 1024  # 绝对体积差：5 GB (触发中途校验的底线)
COOLDOWN_SECONDS = 45 * 60                    # 冷却时间：45 分钟 (保护硬盘，防频繁读写)
SAFE_ZONE_RATIO = 0.95                        # 终点免打扰区：95% (最后 5% 冲刺期绝不干预)
REQ_TIMEOUT = 10                              # API请求超时时间（秒）
CHECK_STUCK_TIMEOUT = 180                     # 校验卡在 0.0% 无变化的超时自救时间（秒）

# ================= 动态路径解析 =================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, 'merger.log')
LOCK_FILE_PATH = '/tmp/pt_merger_v4.lock'
# ==============================================

# ================= 日志系统配置 =================
log_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=5*1024*1024, backupCount=1, encoding='utf-8')
log_formatter = logging.Formatter('%(asctime)s - %(message)s')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger("PT_Merger")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
# ==============================================

# 内存数据库：记录每个哈希值的“上一次强制校验时间”
cooldown_db = {}

# 待恢复集合：记录由本脚本发起暂停+校验、等待校验结束后自动恢复(start)的种子 hash
# 结构: { hash_str: {"name": name, "action": action_name, "trigger_time": ts} }
pending_resume_db = {}

# 校验进度看门狗：监测是否有种子卡在 checking 进度纹丝不动
# 结构: { hash_str: {"progress": float, "since": ts, "name": name} }
checking_watchdog_db = {}

CHECKING_STATES = ('checkingUP', 'checkingDL', 'checkingResumeData')
STOPPED_STATES = ('stoppedDL', 'stoppedUP', 'pausedDL', 'pausedUP')
ACTIVE_DL_STATES = ('downloading', 'stalledDL', 'metaDL', 'queuedDL', 'forcedDL')


def acquire_single_instance_lock():
    """通过文件锁确保全局只有一个脚本进程在运行，防止双开并发轰炸 qBittorrent"""
    lock_fd = open(LOCK_FILE_PATH, 'w')
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(str(os.getpid()))
        lock_fd.flush()
        return lock_fd
    except IOError:
        print("检测到另一个 pt_merger_v4.py 实例正在运行，当前进程自动退出。")
        sys.exit(0)


def qb_stop_torrents(session, hashes_str):
    """兼容 qBittorrent v5.x (stop) 与 v4.x (pause)"""
    resp = session.post(f"{QB_URL}/api/v2/torrents/stop", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    if resp.status_code == 404:
        resp = session.post(f"{QB_URL}/api/v2/torrents/pause", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    return resp


def qb_start_torrents(session, hashes_str):
    """兼容 qBittorrent v5.x (start) 与 v4.x (resume)"""
    resp = session.post(f"{QB_URL}/api/v2/torrents/start", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    if resp.status_code == 404:
        resp = session.post(f"{QB_URL}/api/v2/torrents/resume", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    return resp


def qb_recheck_torrents(session, hashes_str):
    """触发 qBittorrent 重新校验"""
    return session.post(f"{QB_URL}/api/v2/torrents/recheck", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)


def wait_until_stopped(session, target_hashes, max_wait=10):
    """等待目标种子真正进入 stopped 状态后再发 recheck，防止底层状态机竞态死锁"""
    target_set = set(target_hashes)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            hashes_param = "|".join(target_hashes)
            resp = session.get(f"{QB_URL}/api/v2/torrents/info", params={"hashes": hashes_param}, timeout=REQ_TIMEOUT)
            if resp.status_code == 200:
                info_list = resp.json()
                if all(t['state'] in STOPPED_STATES or t['state'] in CHECKING_STATES for t in info_list if t['hash'] in target_set):
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def trigger_safe_recheck(session, hash_list, name, action_name):
    """
    安全两段式校验（第一段）：
    1. 先调用 stop 停止种子并等待落盘确认进入 stopped 状态；
    2. 调用 recheck 进入校验队列；
    3. 登记到 pending_resume_db，不在同一秒内强发 start，等主循环检测到校验完成后再自动 start！
    """
    if not hash_list:
        return

    hashes_str = "|".join(hash_list)
    logger.info(f"    [-] 执行停止 (切断汇报 & 刷入磁盘缓存): {len(hash_list)} 个任务...")
    qb_stop_torrents(session, hashes_str)

    # 等待 qBittorrent 将任务真正标记为 stoppedDL/stoppedUP，并留出缓冲时间刷盘
    wait_until_stopped(session, hash_list, max_wait=8)
    time.sleep(2)

    # 先全部登记到待恢复队列，然后逐个或批量触发校验
    now = time.time()
    for h in hash_list:
        pending_resume_db[h] = {
            "name": name,
            "action": action_name,
            "trigger_time": now
        }
        cooldown_db[h] = now

    logger.info(f"    [~] 提交硬盘校验 ({action_name})，将在校验完成后自动恢复启动...")
    qb_recheck_torrents(session, hashes_str)


def process_pending_resumes_and_watchdog(session, all_torrents):
    """
    1. 安全两段式校验（第二段）：检查 pending_resume_db 中已脱离 checking 状态的种子，自动调用 start 恢复；
    2. 校验死锁看门狗：如果发现任何种子处于 checkingDL/checkingUP 且进度持续 CHECK_STUCK_TIMEOUT 秒完全不动，
       自动执行 stop -> start 解除假死。
    """
    now = time.time()
    torrent_map = {t['hash']: t for t in all_torrents}

    # --- A. 检查待恢复任务 ---
    to_start = []
    for h, meta in list(pending_resume_db.items()):
        t = torrent_map.get(h)
        if not t:
            del pending_resume_db[h]
            continue

        state = t['state']
        # 刚刚触发的前 5 秒内可能还没变成 checking 状态，先等待
        if state in CHECKING_STATES:
            continue

        if state in STOPPED_STATES:
            # 校验已经完成并停留在 stopped 状态（或者已等待超过 5 秒仍处于 stopped），立即恢复启动
            if now - meta['trigger_time'] >= 5:
                to_start.append((h, meta, t['progress']))
                del pending_resume_db[h]
        else:
            # 已经处于 downloading / uploading / stalledUP 等活动状态，无需再恢复
            logger.info(f"    [+] 任务已恢复活动状态 ({state}, {t['progress']*100:.1f}%): {meta['name']}")
            del pending_resume_db[h]

    if to_start:
        hashes_str = "|".join(h for h, _, _ in to_start)
        qb_start_torrents(session, hashes_str)
        for h, meta, prog in to_start:
            logger.info(f"    [+] 校验完毕，已自动恢复任务 ({meta['action']} -> 当前进度 {prog*100:.1f}%): {meta['name']}")

    # --- B. 校验死锁看门狗 ---
    current_checking_hashes = set()
    for t in all_torrents:
        if t['state'] in CHECKING_STATES:
            h = t['hash']
            prog = t.get('progress', 0.0)
            current_checking_hashes.add(h)

            if h not in checking_watchdog_db:
                checking_watchdog_db[h] = {"progress": prog, "since": now, "name": t['name']}
            else:
                record = checking_watchdog_db[h]
                if abs(prog - record["progress"]) > 0.0001:
                    # 进度有推进，刷新时间戳
                    record["progress"] = prog
                    record["since"] = now

    # 清理已经不在 checking 状态的记录
    for h in list(checking_watchdog_db.keys()):
        if h not in current_checking_hashes:
            del checking_watchdog_db[h]

    # 检查是否存在进度停滞超时的校验任务
    # 注意：如果队列里有多个 checking 任务，qBittorrent 默认每次只校验 1 个，
    # 因此只有当【所有】checking 任务都没有任何进度变化超过阈值时，才判定为引擎死锁！
    if current_checking_hashes:
        any_progressing = any((now - checking_watchdog_db[h]["since"]) < CHECK_STUCK_TIMEOUT for h in current_checking_hashes)
        if not any_progressing:
            stuck_hashes = list(current_checking_hashes)
            stuck_names = {checking_watchdog_db[h]["name"] for h in stuck_hashes}
            logger.warning(f"🚨 [看门狗自救] 检测到 {len(stuck_hashes)} 个任务卡在校验状态超过 {CHECK_STUCK_TIMEOUT} 秒无任何进度变化: {', '.join(stuck_names)}")
            hashes_str = "|".join(stuck_hashes)
            logger.warning("    [!] 正在执行 Stop -> 等待 3 秒 -> Start 解锁状态机...")
            qb_stop_torrents(session, hashes_str)
            time.sleep(3)
            qb_start_torrents(session, hashes_str)
            for h in stuck_hashes:
                checking_watchdog_db.pop(h, None)
                pending_resume_db.pop(h, None)
                cooldown_db[h] = now  # 刚自救完进入冷却，避免立刻又被触发 recheck
            return True  # 返回 True 表示本轮刚触发了看门狗自救，跳过后续干预

    return False


def run_ultimate_merger_v4():
    lock_fd = acquire_single_instance_lock()  # noqa: F841 保持引用防止被 GC 释放锁
    session = requests.Session()

    logger.info("正在连接 qBittorrent...")
    login_resp = session.post(f"{QB_URL}/api/v2/auth/login", data={"username": USERNAME, "password": PASSWORD}, timeout=REQ_TIMEOUT)
    if login_resp.text != "Ok.":
        logger.error(f"登录失败！请检查配置。响应: {login_resp.status_code} {login_resp.text}")
        return
    logger.info(f"登录成功 (PID={os.getpid()})！V4.1 防死锁智能调度系统已上线...")

    while True:
        try:
            response = session.get(f"{QB_URL}/api/v2/torrents/info", timeout=REQ_TIMEOUT)

            if response.status_code == 403:
                logger.warning("⚠️ 登录凭证已过期，正在尝试重新登录...")
                session.post(f"{QB_URL}/api/v2/auth/login", data={"username": USERNAME, "password": PASSWORD}, timeout=REQ_TIMEOUT)
                continue

            all_torrents = response.json()

            # 1. 处理已校验完待恢复(start)的种子 & 运行校验卡死看门狗
            if process_pending_resumes_and_watchdog(session, all_torrents):
                time.sleep(15)
                continue

            # 2. 全局校验限流保护：如果当前已经有任务正在校验，等待其完成，不叠加新的 recheck 请求
            global_checking = [t for t in all_torrents if t['state'] in CHECKING_STATES]
            if global_checking:
                checking_names = list({t['name'] for t in global_checking})
                logger.info(f"[全局队列] 当前有 {len(global_checking)} 个任务正在进行校验 ({checking_names[0]} 等)，本轮等待校验完成...")
                time.sleep(15)
                continue

            # 3. 以特征值为 Key 进行分组
            groups = {}
            for t in all_torrents:
                # 基因序列：名字 + 总大小 + 分块大小(若有) + 保存路径
                key = (t['name'], t['size'], t.get('piece_size', 0), t['save_path'])
                if key not in groups:
                    groups[key] = []
                groups[key].append(t)

            # 4. 遍历寻找双胞胎/多胞胎
            for key, siblings in groups.items():
                if len(siblings) < 2:
                    continue

                if any(t['state'] in CHECKING_STATES or t['hash'] in pending_resume_db for t in siblings):
                    continue

                # 如果这个组里【没有任何一个】任务处于未完成下载/暂停待同步状态，说明大家都下完了，跳过
                if not any(t['state'] in ACTIVE_DL_STATES or (t['state'] in STOPPED_STATES and t['progress'] < 1.0) for t in siblings):
                    continue
                # 如果所有未完成的任务都是用户手动停止的，且组内没有正在下载的任务，也跳过（除非有 100% 种带飞下载中任务）
                if not any(t['state'] in ACTIVE_DL_STATES for t in siblings):
                    continue

                name, total_size, piece_size, save_path = key

                # ============ 进度联合计算 ============
                has_perfect_seed = any(t['progress'] == 1.0 for t in siblings)

                global_ratio = 0.0
                if has_perfect_seed:
                    global_ratio = 1.0
                else:
                    all_states = [session.get(f"{QB_URL}/api/v2/torrents/pieceStates?hash={s['hash']}", timeout=REQ_TIMEOUT).json() for s in siblings]
                    if not all_states or not all_states[0]:
                        continue
                    total_pieces = len(all_states[0])
                    completed_pieces = 0
                    for piece_tuple in zip(*all_states):
                        if 2 in piece_tuple:
                            completed_pieces += 1
                    global_ratio = completed_pieces / total_pieces
                    logger.info(f"[{name}] 实时联合进度: {global_ratio * 100:.2f}%")

                # ============ 瀑布流调度逻辑 ============

                # 【优先级 1】大结局：联合进度达到 100%
                if global_ratio == 1.0:
                    now = time.time()
                    hashes_to_fix = [
                        s['hash'] for s in siblings
                        if s['progress'] < 1.0 and (now - cooldown_db.get(s['hash'], 0) > 300)
                    ]
                    if hashes_to_fix:
                        logger.info(f"[大结局] 100% 拼图完成或匹配到本地做种: {name} (待校验: {len(hashes_to_fix)} 个)")
                        trigger_safe_recheck(session, hashes_to_fix, name, "满血做种")
                        break  # 本轮已提交校验任务，退出分组循环等待校验队列完成
                    continue

                # 【判断 2】进入最后冲刺安全区？
                if global_ratio >= SAFE_ZONE_RATIO:
                    continue

                # 【优先级 2】中途补给站：落后太多，拉一把
                # 收集本组内所有满足“落后超过 5GB 且冷却时间已到”的种子，一次性停下或逐个校验，绝不在循环内无脑连发
                now = time.time()
                lagging_siblings = []
                for s in siblings:
                    local_ratio = s['progress']
                    gap_bytes = (global_ratio - local_ratio) * total_size
                    if gap_bytes >= GAP_THRESHOLD_BYTES:
                        hash_str = s['hash']
                        last_check_time = cooldown_db.get(hash_str, 0)
                        if now - last_check_time > COOLDOWN_SECONDS:
                            lagging_siblings.append((gap_bytes, s))

                if lagging_siblings:
                    # 优先挑选落后最多的 1 个（或同一组落后种子）进行安全校验，每次只挑落后最严重的 1 个校验，彻底避免多任务排队假死
                    lagging_siblings.sort(key=lambda x: x[0], reverse=True)
                    gap_bytes, target_s = lagging_siblings[0]
                    gap_gb = gap_bytes / (1024**3)
                    target_hash = target_s['hash']
                    logger.info(f"[中途同步] 发现严重落后 (差额 {gap_gb:.1f} GB, 进度 {target_s['progress']*100:.1f}% -> 目标 {global_ratio*100:.1f}%): {name}")
                    trigger_safe_recheck(session, [target_hash], name, f"追赶进度至 {global_ratio*100:.1f}%")
                    break  # 每次只推进一个校验任务，等它校验并恢复后再处理下一个

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ 遭遇网络波动或 qBit 卡顿，本轮检测已跳过。原因: {e}")
        except Exception as e:
            logger.error(f"❌ 捕获到内部错误: {e}")

        time.sleep(15)


if __name__ == "__main__":
    run_ultimate_merger_v4()
