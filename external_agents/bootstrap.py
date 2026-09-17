"""Download pinned sources and install locked native CLI releases locally."""
import json
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parent
versions = json.loads((root/'versions.json').read_text())
for name, source in versions['sources'].items():
    target = root/'repos'/name
    if not target.exists():
        subprocess.run(['git', 'clone', '--no-checkout', '--filter=blob:none', source['url'], str(target)], check=True)
        subprocess.run(['git', '-C', str(target), 'checkout', source['commit']], check=True)
    actual = subprocess.check_output(['git', '-C', str(target), 'rev-parse', 'HEAD'], text=True).strip()
    if actual != source['commit']:
        raise SystemExit(f'{name}: unexpected checkout {actual}; not overwriting local source')
subprocess.run(['npm', 'ci', '--prefix', str(root/'runtime')], check=True)
