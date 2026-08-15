import os
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

# ================= 动态路径解析 =================
# 自动获取当前脚本（pt_merger.py）所在的绝对目录路径
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 将日志文件名与脚本所在目录无缝拼接
LOG_FILE_PATH = os.path.join(SCRIPT_DIR, 'merger.log')
# ==============================================

# ================= 日志系统配置 =================
# 自动在脚本同目录下创建 merger.log，最大 5MB，保留 1 个备份
log_handler = RotatingFileHandler(LOG_FILE_PATH, maxBytes=5*1024*1024, backupCount=1, encoding='utf-8')
log_formatter = logging.Formatter('%(asctime)s - %(message)s')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger("PT_Merger")
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)
# ==============================================

# 内存数据库：用于记录每个哈希值的“上一次强制校验时间”
cooldown_db = {}

def run_ultimate_merger_v4():
    session = requests.Session()
    
    logger.info("正在连接 qBittorrent...")
    if session.post(f"{QB_URL}/api/v2/auth/login", data={"username": USERNAME, "password": PASSWORD}, timeout=REQ_TIMEOUT).text != "Ok.":
        logger.error("登录失败！请检查配置。")
        return
    logger.info("登录成功！V4.0 企业级智能调度系统已上线...")

    while True:
        try:
            # 获取请求的原始响应
            response = session.get(f"{QB_URL}/api/v2/torrents/info", timeout=REQ_TIMEOUT)
            
            # 如果被踢下线 (403) 或者不是 200，重新登录！
            if response.status_code == 403:
                logger.warning("⚠️ 登录凭证已过期，正在尝试重新登录...")
                session.post(f"{QB_URL}/api/v2/auth/login", data={"username": USERNAME, "password": PASSWORD}, timeout=REQ_TIMEOUT)
                continue # 登录完直接进入下一轮大循环
                
            all_torrents = response.json()
            
            # 以特征值为 Key 进行分组
            groups = {}
            for t in all_torrents:
                # 基因序列：名字 + 总大小 + 分块大小(若有) + 保存路径
                key = (t['name'], t['size'], t.get('piece_size', 0), t['save_path']) 
                if key not in groups:
                    groups[key] = []
                groups[key].append(t)

            # 遍历寻找双胞胎/多胞胎
            for key, siblings in groups.items():
                if len(siblings) < 2:
                    continue  # 独生子女，跳过
                
                # 如果这个组里【没有任何一个】任务处于下载中，说明大家都下完了，跳过
                if not any(t['state'] in ['downloading', 'stalledDL', 'metaDL'] for t in siblings):
                    continue

                name, total_size, piece_size, save_path = key
                
                # ============ 进度联合计算 ============
                # 看看组里是不是已经有一位大佬 100% 完成了（反向匹配已完成列表）
                has_perfect_seed = any(t['progress'] == 1.0 for t in siblings)
                
                global_ratio = 0.0
                if has_perfect_seed:
                    global_ratio = 1.0  # 大佬带飞，全局进度直接认定为 100%
                else:
                    # 只有都没下完时，才去拉取 pieceStates 算按位或运算
                    all_states = [session.get(f"{QB_URL}/api/v2/torrents/pieceStates?hash={s['hash']}", timeout=REQ_TIMEOUT).json() for s in siblings]
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
                    hashes_to_fix = [s['hash'] for s in siblings if s['progress'] < 1.0]
                    if hashes_to_fix:
                        logger.info(f"[大结局] 100% 拼图完成或匹配到本地做种: {name}")
                        execute_magic_combo(session, hashes_to_fix, "满血做种")
                    continue # 满进度处理完，直接进入下一个组

                # 【判断 2】进入最后冲刺安全区？
                if global_ratio >= SAFE_ZONE_RATIO:
                    continue # 在最后 5%，绝不干预，打死也不走优先级 2

                # 【优先级 2】中途补给站：落后太多，拉一把
                for s in siblings:
                    local_ratio = s['progress']
                    gap_bytes = (global_ratio - local_ratio) * total_size
                    
                    # 检查差值阈值 (落后是否超过 5GB)
                    if gap_bytes >= GAP_THRESHOLD_BYTES:
                        hash_str = s['hash']
                        last_check_time = cooldown_db.get(hash_str, 0)
                        
                        # 检查冷却时间阈值 (是否超过 45 分钟)
                        if time.time() - last_check_time > COOLDOWN_SECONDS:
                            gap_gb = gap_bytes / (1024**3)
                            logger.info(f"[中途同步] 发现严重落后 (差额 {gap_gb:.1f} GB): {name}")
                            execute_magic_combo(session, [hash_str], f"追赶进度至 {global_ratio*100:.1f}%")
                            
                            # 刷新该任务的冷却时间戳
                            cooldown_db[hash_str] = time.time()

        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ 遭遇网络波动或 qBit 卡顿，本轮检测已跳过。原因: {e}") 
        except Exception as e:
            logger.error(f"❌ 捕获到内部错误: {e}")
            
        time.sleep(15) # 每 15 秒扫一次盘

def execute_magic_combo(session, hash_list, action_name):
    """
    执行不可拆分的【三连击】指令：暂停 ➔ 校验 ➔ 恢复
    """
    hashes_str = "|".join(hash_list)
    logger.info("    [-] 执行强制暂停 (切断汇报)...")
    session.post(f"{QB_URL}/api/v2/torrents/pause", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    
    time.sleep(3) # 留出 3 秒，让底层 I/O 引擎有充足时间把内存数据刷入硬盘
    
    logger.info("    [~] 执行硬盘校验 (盘点数据)...")
    session.post(f"{QB_URL}/api/v2/torrents/recheck", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    
    # 瞬间给底层引擎发 Resume，引擎会自动排队，等校验走完直接满血复活
    logger.info(f"    [+] 预置恢复指令 ({action_name})...")
    session.post(f"{QB_URL}/api/v2/torrents/resume", data={"hashes": hashes_str}, timeout=REQ_TIMEOUT)
    logger.info("    执行完毕！")

if __name__ == "__main__":
    run_ultimate_merger_v4()
