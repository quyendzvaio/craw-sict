"""scripts/deploy_alloy_vm.sh: hai lỗi lộ ra lần đầu chạy trên VM2 thật (2026-11-05, CD deploy_alloy)."""
import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy_alloy_vm.sh"
SRC = SCRIPT.read_text(encoding="utf-8")


def test_bash_syntax():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)


def test_stdin_group_has_no_and_list_that_fails_on_vm2():
    # `[ "$VM" = vm1 ] && printf …` là lệnh cuối của nhóm { …; } trong pipeline: với vm2 trả 1 → pipefail → exit 1
    # dù Alloy đã lên và đang gửi dữ liệu
    group = SRC[SRC.index("{ printf 'GCLOUD_RW_API_KEY"):SRC.index('} | "${SSH[@]}"')]
    assert not re.search(r'\[ "\$VM" = vm1 \] &&', group)
    assert 'if [ "$VM" = vm1 ]; then' in group


def test_vm2_scrapes_embed_on_embed_bind():
    # embed nghe EMBED_BIND (IP private), không nghe localhost → up{job="sict/embed"} = 0 nếu scrape localhost:8090
    assert "EMBED_BIND" in SRC and "--embed-addr" in SRC
    block = SRC[SRC.index('if [ "$VM" = vm2 ]; then'):SRC.index('python3 "$ROOT/scripts/render_alloy.py"')]
    assert "--embed-addr" in block and ":8090" in block
