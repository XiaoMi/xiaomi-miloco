"""install-hermes.sh helper: 读 config.json 的 python_bin 字段"""
import json, sys
try:
    d = json.load(open(sys.argv[1] + '/config.json'))
    print(d.get('server', {}).get('python_bin', '') or '')
except Exception:
    print('')
