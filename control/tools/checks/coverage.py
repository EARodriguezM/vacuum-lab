"""Which lines of the rig's own code the selftest never ran.

Built on `sys.monitoring` (Python 3.12+): each line reports itself once and
is then switched off, so the cost is paid on first execution only and the
timing sections still see real timing -- which `python -m trace` does not
allow. Lines are counted per file under lab/ and drivers/; a line is
executable when its compiled code says so, so docstrings and comments are
never "missed".
"""

from __future__ import annotations

import os
import sys
import time

PACKAGES = ("lab", "drivers")


class Coverage:
    def __init__(self, root: str):
        self.root = root
        self.prefixes = tuple(os.path.join(root, p) + os.sep for p in PACKAGES)
        self.hit: dict[str, set[int]] = {}
        self._tool = None
        self._started = 0.0

    def start(self) -> bool:
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is None:
            print("coverage needs Python 3.12 or newer; not measured")
            return False
        tool = monitoring.COVERAGE_ID
        monitoring.use_tool_id(tool, "selftest")
        monitoring.register_callback(tool, monitoring.events.LINE, self._line)
        monitoring.set_events(tool, monitoring.events.LINE)
        self._tool = tool
        self._started = time.time()
        return True

    def stop(self) -> None:
        if self._tool is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self._tool, 0)
        monitoring.register_callback(self._tool, monitoring.events.LINE, None)
        monitoring.free_tool_id(self._tool)
        self._tool = None

    def _line(self, code, line):
        name = code.co_filename
        if name.startswith(self.prefixes):
            self.hit.setdefault(name, set()).add(line)
        return sys.monitoring.DISABLE

    # --- the report -------------------------------------------------------

    @staticmethod
    def executable(path: str) -> set[int]:
        with open(path, encoding="utf-8") as fh:
            code = compile(fh.read(), path, "exec")
        lines: set[int] = set()
        stack = [code]
        while stack:
            current = stack.pop()
            lines.update(line for _, _, line in current.co_lines()
                         if line is not None)
            stack.extend(c for c in current.co_consts if hasattr(c, "co_lines"))
        return lines

    def files(self) -> list[str]:
        found = []
        for package in PACKAGES:
            for folder, _, names in os.walk(os.path.join(self.root, package)):
                found += [os.path.join(folder, n) for n in names
                          if n.endswith(".py")]
        return sorted(found)

    def report(self, out_path: str | None) -> None:
        rows, detail, changed = [], [], []
        total_lines = total_hit = 0
        for path in self.files():
            if os.path.getmtime(path) > self._started:
                # Edited while it ran: its line numbers no longer match.
                changed.append(os.path.relpath(path, self.root))
            lines = self.executable(path)
            hit = self.hit.get(path, set()) & lines
            missed = sorted(lines - hit)
            total_lines += len(lines)
            total_hit += len(hit)
            relative = os.path.relpath(path, self.root)
            share = 100.0 * len(hit) / len(lines) if lines else 100.0
            rows.append((relative, len(lines), len(hit), share))
            if missed:
                detail.append(f"{relative}: {_ranges(missed)}")
        print("\nCoverage of lab/ and drivers/ (lines run at least once)")
        for relative, count, hit, share in rows:
            print(f"  {share:5.1f} %  {hit:5d} / {count:<5d} {relative}")
        overall = 100.0 * total_hit / total_lines if total_lines else 100.0
        print(f"  {overall:5.1f} %  {total_hit:5d} / {total_lines:<5d} all")
        if changed:
            print("  changed during the run, so not to be trusted: "
                  + ", ".join(changed))
        if out_path:
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write("Lines never run, by file\n\n")
                fh.write("\n".join(detail) + "\n")
            print(f"  lines never run: {out_path}")


def _ranges(numbers: list[int]) -> str:
    spans, start, last = [], None, None
    for n in numbers:
        if start is None:
            start = last = n
        elif n == last + 1:
            last = n
        else:
            spans.append(f"{start}" if start == last else f"{start}-{last}")
            start = last = n
    if start is not None:
        spans.append(f"{start}" if start == last else f"{start}-{last}")
    return ", ".join(spans)
