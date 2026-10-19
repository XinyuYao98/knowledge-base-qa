"""数据生命周期清理任务（架构 §6.3）。

只做两件事，**不做更多**：

| 任务 | 频率 | 动作 |
|---|---|---|
| `cleanup_staging` | 每天 | 删 `data/staging/` 下 mtime > 7 天的目录 |
| `cleanup_uploads` | 每小时 | 删 `data/uploads/` 下超过 24h 的分片 |

## 为什么不清理 query_logs

按 5 用户 × 每天 50 次估算，一年约 9 万条、< 100MB。
**demo 场景到不了需要清理的量级，加清理反而增加出错可能。**
这写进 README 的已知限制，而不是偷偷加个定时删除。

## 安全设计

1. **只删过期，不删「最近」**：mtime 判定，且有最小年龄保护，防止刚创建就被删
2. **不跟随符号链接**：避免清理任务把链接目标删掉
3. **dry-run 默认开启**：`--apply` 才真删。第一次运行建议先看清单
"""

from __future__ import annotations

import argparse
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("cleanup")

#: 最小年龄保护：即使配置错了，也不删最近 1 小时内的东西
MIN_AGE_SECONDS = 3600


@dataclass(slots=True)
class CleanupResult:
    task: str
    scanned: int = 0
    deleted: int = 0
    freed_bytes: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True

    def to_dict(self) -> dict:
        return {
            "task": self.task,
            "scanned": self.scanned,
            "deleted": self.deleted,
            "freed_bytes": self.freed_bytes,
            "freed_mb": round(self.freed_bytes / 1024 / 1024, 2),
            "errors": self.errors,
            "dry_run": self.dry_run,
        }


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def cleanup_staging(
    *,
    retention_days: int | None = None,
    dry_run: bool = True,
    root: Path | None = None,
) -> CleanupResult:
    """删除过期的管道中间产物。"""
    days = retention_days if retention_days is not None else settings.STAGING_RETENTION_DAYS
    base = root or settings.staging_dir
    result = CleanupResult(task="cleanup_staging", dry_run=dry_run)

    if not base.exists():
        return result

    cutoff = time.time() - max(days * 86400, MIN_AGE_SECONDS)

    for entry in base.iterdir():
        if entry.is_symlink() or not entry.is_dir():
            continue
        result.scanned += 1
        try:
            mtime = entry.stat().st_mtime
        except OSError as e:
            result.errors.append(f"{entry.name}: {e}")
            continue

        if mtime >= cutoff:
            continue

        size = _dir_size(entry)
        if dry_run:
            log.info(
                "cleanup_would_delete",
                path=str(entry.name),
                age_days=round((time.time() - mtime) / 86400, 1),
            )
            result.deleted += 1
            result.freed_bytes += size
            continue

        try:
            shutil.rmtree(entry)
            result.deleted += 1
            result.freed_bytes += size
            log.info("cleanup_deleted", path=str(entry.name), freed_mb=round(size / 1024 / 1024, 2))
        except OSError as e:
            result.errors.append(f"{entry.name}: {e}")

    return result


def cleanup_uploads(
    *,
    retention_hours: int | None = None,
    dry_run: bool = True,
    root: Path | None = None,
) -> CleanupResult:
    """删除过期的上传分片与未完成的临时文件。"""
    hours = retention_hours if retention_hours is not None else settings.UPLOAD_PART_RETENTION_HOURS
    base = root or settings.uploads_dir
    result = CleanupResult(task="cleanup_uploads", dry_run=dry_run)

    if not base.exists():
        return result

    cutoff = time.time() - max(hours * 3600, MIN_AGE_SECONDS)

    for entry in base.iterdir():
        if entry.is_symlink():
            continue
        result.scanned += 1
        try:
            mtime = entry.stat().st_mtime
        except OSError as e:
            result.errors.append(f"{entry.name}: {e}")
            continue

        if mtime >= cutoff:
            continue

        size = _dir_size(entry) if entry.is_dir() else entry.stat().st_size
        if dry_run:
            result.deleted += 1
            result.freed_bytes += size
            continue

        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            result.deleted += 1
            result.freed_bytes += size
        except OSError as e:
            result.errors.append(f"{entry.name}: {e}")

    return result


def run_all(dry_run: bool = True) -> dict:
    staging = cleanup_staging(dry_run=dry_run)
    uploads = cleanup_uploads(dry_run=dry_run)
    return {
        "staging": staging.to_dict(),
        "uploads": uploads.to_dict(),
        "total_freed_mb": round((staging.freed_bytes + uploads.freed_bytes) / 1024 / 1024, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="数据生命周期清理")
    ap.add_argument("--apply", action="store_true", help="真正执行删除（默认 dry-run）")
    ap.add_argument(
        "--task",
        choices=["staging", "uploads", "all"],
        default="all",
        help="只跑指定任务",
    )
    ap.add_argument("--staging-days", type=int, default=None)
    ap.add_argument("--upload-hours", type=int, default=None)
    args = ap.parse_args()

    dry = not args.apply

    print("=" * 64)
    print(f"数据清理{'（dry-run，不会真删）' if dry else '（已应用）'}")
    print("=" * 64)

    if args.task in ("staging", "all"):
        r = cleanup_staging(retention_days=args.staging_days, dry_run=dry)
        print(f"\n[staging] 保留 {args.staging_days or settings.STAGING_RETENTION_DAYS} 天")
        print(f"  扫描 {r.scanned} 个目录，{'将删除' if dry else '已删除'} {r.deleted} 个，"
              f"释放 {r.freed_bytes/1024/1024:.1f} MB")
        for e in r.errors[:5]:
            print(f"  ! {e}")

    if args.task in ("uploads", "all"):
        r2 = cleanup_uploads(retention_hours=args.upload_hours, dry_run=dry)
        print(f"\n[uploads] 保留 {args.upload_hours or settings.UPLOAD_PART_RETENTION_HOURS} 小时")
        print(f"  扫描 {r2.scanned} 个条目，{'将删除' if dry else '已删除'} {r2.deleted} 个，"
              f"释放 {r2.freed_bytes/1024/1024:.1f} MB")
        for e in r2.errors[:5]:
            print(f"  ! {e}")

    if dry:
        print("\n提示：加 --apply 才会真正删除")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
