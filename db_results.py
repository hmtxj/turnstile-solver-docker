"""
任务结果的内存存储（增强版）

支持结果上限保护、主动删除和统计功能。
高并发下不会无限增长内存。
"""
import time
import threading

# 内存数据库
results_db: dict = {}
# 结果上限（超出后自动清理最旧的）
MAX_RESULTS = 1000
# 统计计数器
_stats = {"total": 0, "success": 0, "failed": 0}
# 线程安全锁（Quart 在 asyncio 中运行，但以防万一）
_lock = threading.Lock()


async def init_db():
    print("[系统] 结果数据库初始化成功 (内存模式，上限 1000 条)")


async def save_result(task_id: str, task_type: str, data: dict):
    """存储任务结果，超出上限自动清理最旧记录"""
    data["createTime"] = time.time()
    with _lock:
        results_db[task_id] = data
        _stats["total"] += 1
        value = data.get("value", "")
        if value and value != "CAPTCHA_FAIL":
            _stats["success"] += 1
        elif value == "CAPTCHA_FAIL":
            _stats["failed"] += 1
        # 超出上限时清理最旧的 20%
        if len(results_db) > MAX_RESULTS:
            sorted_keys = sorted(
                results_db.keys(),
                key=lambda k: results_db[k].get("createTime", 0)
            )
            to_remove = sorted_keys[:len(sorted_keys) // 5]
            for k in to_remove:
                del results_db[k]
    short_val = str(value)[:20] if isinstance(value, str) else str(value)
    print(f"[系统] 任务 {task_id[:8]}... 状态更新: {short_val}")


async def load_result(task_id: str) -> dict | None:
    """读取任务结果"""
    return results_db.get(task_id)


async def delete_result(task_id: str):
    """删除已取走的结果，释放内存"""
    with _lock:
        results_db.pop(task_id, None)


async def cleanup_old_results(days_old: int = 1) -> int:
    """清理过期结果"""
    now = time.time()
    with _lock:
        to_delete = [
            tid for tid, res in results_db.items()
            if isinstance(res, dict) and now - res.get("createTime", now) > days_old * 86400
        ]
        for tid in to_delete:
            del results_db[tid]
    return len(to_delete)


async def get_stats() -> dict:
    """返回统计信息"""
    return {
        "total_tasks": _stats["total"],
        "success": _stats["success"],
        "failed": _stats["failed"],
        "success_rate": round(_stats["success"] / max(_stats["total"], 1) * 100, 1),
        "pending_results": len(results_db),
    }
