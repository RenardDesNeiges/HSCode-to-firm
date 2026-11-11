"""Threaded execution and progress display for the 10-K parser."""

from __future__ import annotations

import shutil
import sys
import textwrap
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from tqdm import tqdm


def _format_elapsed(seconds):
    seconds = int(max(0.0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _auto_queue_lines(workers):
    return max(1, min(workers + 2, shutil.get_terminal_size().lines - 3))


def _queue_line(row, status, elapsed, width):
    _, cik, conm, fyear, _ = row
    if status == "run":
        prefix = f"[run  {_format_elapsed(elapsed):>8}] "
    else:
        prefix = "[next        ] "
    room = max(8, width - len(prefix) - 16)
    conm = textwrap.shorten(str(conm), width=room, placeholder="…")
    return f"{prefix}{cik:<10} {conm:<{room}} {fyear:>4}"


class _QueuePanel:
    def __init__(self, line_count):
        self.bars = [
            tqdm(
                total=0,
                position=index + 1,
                bar_format="{desc}",
                desc="",
                leave=False,
                dynamic_ncols=True,
            )
            for index in range(line_count)
        ]

    def update(self, running, waiting):
        width = shutil.get_terminal_size().columns
        lines = [
            _queue_line(row, "run", time.monotonic() - started, width)
            for row, started in running[: len(self.bars)]
        ]
        for row in waiting[: len(self.bars) - len(lines)]:
            lines.append(_queue_line(row, "next", 0, width))
        for bar, line in zip(self.bars, lines):
            bar.set_description_str(line, refresh=False)
        for bar in self.bars:
            bar.refresh()

    def close(self):
        for bar in self.bars:
            bar.close()


def _commit_finished(in_flight, on_result, on_error, stats):
    for future, (row, _) in list(in_flight.items()):
        if future.cancelled() or not future.done():
            continue
        try:
            result = future.result()
        except Exception as exc:
            bucket = on_error(row, exc)
        else:
            bucket = on_result(row, result)
        if bucket:
            stats[bucket] = stats.get(bucket, 0) + 1


def run_parallel(
    rows,
    worker,
    on_result,
    on_error,
    stats,
    workers=8,
    queue_lines=None,
    desc="Processing",
    cost_fn=None,
):
    for key in ("ok", "empty", "error"):
        stats.setdefault(key, 0)
    if queue_lines is None:
        queue_lines = _auto_queue_lines(workers)

    executor = ThreadPoolExecutor(max_workers=workers)
    pbar = tqdm(
        total=len(rows),
        desc=desc,
        unit="filing",
        smoothing=0,
        position=0,
        dynamic_ncols=True,
        bar_format=(
            "{l_bar}{bar}| {n_fmt}/{total_fmt} "
            "[{elapsed}<{remaining}, {rate_inv_fmt}{postfix}]"
        ),
    )
    panel = _QueuePanel(queue_lines if sys.stderr.isatty() else 0)

    in_flight = {}
    next_index = 0

    def submit_next():
        nonlocal next_index
        if next_index >= len(rows):
            return False
        row = rows[next_index]
        next_index += 1
        in_flight[executor.submit(worker, row)] = (row, time.monotonic())
        return True

    def update_postfix():
        extra = {"waiting": len(rows) - next_index}
        if cost_fn is not None:
            extra["cost"] = f"${cost_fn():.4f}"
        pbar.set_postfix(**stats, **extra)

    for _ in range(workers):
        if not submit_next():
            break
    update_postfix()

    interrupted = False
    try:
        while in_flight:
            done, _ = wait(
                tuple(in_flight),
                timeout=1.0,
                return_when=FIRST_COMPLETED,
            )
            panel.update(
                list(in_flight.values()),
                rows[next_index : next_index + queue_lines],
            )

            for future in done:
                row, _ = in_flight.pop(future)
                try:
                    result = future.result()
                except Exception as exc:
                    bucket = on_error(row, exc)
                else:
                    bucket = on_result(row, result)
                if bucket:
                    stats[bucket] = stats.get(bucket, 0) + 1
                pbar.update(1)
                submit_next()

            update_postfix()
    except KeyboardInterrupt:
        interrupted = True
        panel.close()
        pbar.close()
        executor.shutdown(wait=False, cancel_futures=True)
        _commit_finished(in_flight, on_result, on_error, stats)
        raise
    finally:
        if not interrupted:
            panel.close()
            pbar.close()
            executor.shutdown(wait=True)

    return stats
