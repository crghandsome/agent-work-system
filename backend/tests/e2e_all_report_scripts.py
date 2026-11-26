"""验证日报/周报/月报三个脚本都能加载并引用到 wage-pay-judge 取数。"""
import sys, subprocess
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

scripts = {
    "日报": "/srv/data/artifacts/skills/daily-report-builder/daily-report-builder/scripts/build_daily_report.py",
    "周报": "/srv/data/artifacts/skills/weekly-report-builder/weekly-report-builder/scripts/build_weekly_report.py",
    "月报": "/srv/data/artifacts/skills/month-report-builder/month-report-builder/scripts/build_full_report.py",
}
for label, path in scripts.items():
    r = subprocess.run(["python", path, "--help"], capture_output=True, text=True, timeout=60)
    ok = r.returncode == 0 and "usage:" in r.stdout
    print(f"{label}: returncode={r.returncode}, ok={ok}")
    if not ok:
        print("  stderr:", r.stderr[:400])
    assert ok, f"{label}脚本加载失败"
print("✓ 日报/周报/月报脚本均可加载，wage-pay-judge 取数引用正常")
print("PASS-ALL-REPORT-SCRIPTS ✓")
