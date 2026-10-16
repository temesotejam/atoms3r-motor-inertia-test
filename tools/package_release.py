from pathlib import Path
import argparse
import hashlib
import json
import shutil
import zipfile
import subprocess
from datetime import datetime, timezone

root = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser()
p.add_argument('--out', required=True, type=Path)
p.add_argument('--framework', required=True, type=Path)
p.add_argument('--build-sha', help='source commit used to build the firmware')
args = p.parse_args()
build = root/'.pio/build/atoms3r'
flash = root/'flash'
images = flash/'firmware'
images.mkdir(exist_ok=True)
for name in ('bootloader.bin','partitions.bin','firmware.bin'):
    shutil.copy2(build/name, images/name)
shutil.copy2(args.framework/'tools/partitions/boot_app0.bin', images/'boot_app0.bin')
parts = [('bootloader.bin',0),('partitions.bin',0x8000),('boot_app0.bin',0xe000),('firmware.bin',0x10000)]
commit = args.build_sha
if not commit:
    result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True, text=True)
    commit = result.stdout.strip() if result.returncode == 0 else 'local-uncommitted'
manifest = {'name':'AtomS3R 電流・回転応答測定', 'version':'1.0.2-20261016-'+commit[:7], 'buildSha':commit,
            'builds':[{'chipFamily':'ESP32-S3','parts':[{'path':'firmware/'+name,'offset':offset} for name,offset in parts]}]}
(flash/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
sums=[]
for path in sorted(images.glob('*.bin')):
    sums.append(hashlib.sha256(path.read_bytes()).hexdigest()+'  firmware/'+path.name)
(flash/'SHA256SUMS.txt').write_text('\n'.join(sums)+'\n',encoding='ascii')
info = {'commit':commit,'version':manifest['version'],'hardware_tested':False,
        'revision':'atoms3r_response_v1_2_20261016','firmware_hashes':sums}
(flash/'build-info.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
args.out.mkdir(parents=True,exist_ok=True)
payload = []
for path in root.rglob('*'):
    rel=path.relative_to(root)
    included = rel.parts[0] in ('src','tests','tools','web','flash','docs','.github') or rel.as_posix() in ('.gitattributes','.gitignore','platformio.ini','README.md','start_flasher.cmd')
    if included and path.is_file() and not any(x in ('.pio','__pycache__','.git','dist','analysis') for x in rel.parts):
        payload.append(path)
archive=args.out/'AtomS3R_電流回転応答測定_20261016.zip'
with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
    for path in payload:
        z.write(path, 'atoms3r-motor-inertia-test/'+path.relative_to(root).as_posix())
with zipfile.ZipFile(archive) as z:
    assert z.testzip() is None
    for path in payload:
        name='atoms3r-motor-inertia-test/'+path.relative_to(root).as_posix()
        assert z.read(name)==path.read_bytes()
record={'created_utc':datetime.now(timezone.utc).isoformat(), 'archive':str(archive),
        'bytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'files':len(payload),'firmware_hashes':sums,'commit':commit,'hardware_tested':False,'uploaded_to_device':False}
(args.out/'atoms3r_current_response_release.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(record,ensure_ascii=False,indent=2))
