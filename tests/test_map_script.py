"""Run the shipped map script with unavailable WebGL; no SDK/network needed."""
import shutil
import subprocess
from pathlib import Path
import pytest


def test_webgl_constructor_failure_is_visible():
    if not shutil.which('node'): pytest.skip('Node.js required')
    html = (Path(__file__).parents[1]/'ui/web/map.html').read_text()
    script = html.split('<script>')[1].split('</script>')[0]
    harness = r'''
const vm = require('vm');
const fs = require('fs');
const status = {textContent: ''};
const context = {window: {}, document: {getElementById: () => status},
  maplibregl: {Map: class {constructor() {throw Error('Failed to initialize WebGL');}}}};
vm.runInNewContext(fs.readFileSync(0, 'utf8'), context);
if (context.window.__mapReady !== false || !context.window.__mapError ||
    !status.textContent.includes('지도 사용 불가')) process.exit(1);
'''
    subprocess.run(['node', '-e', harness], input=script, text=True, check=True)
