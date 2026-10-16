"""Read-only verification of deployed ESP Web Tools manifest and binary hashes."""
import argparse
import hashlib
import json
import time
import urllib.request

p=argparse.ArgumentParser()
p.add_argument('--url',required=True)
p.add_argument('--sha',required=True)
p.add_argument('--attempts',type=int,default=8)
args=p.parse_args()
base=args.url.rstrip('/')+'/'
expected={'firmware/bootloader.bin':0,'firmware/partitions.bin':32768,
          'firmware/boot_app0.bin':57344,'firmware/firmware.bin':65536}

def get(path):
    req=urllib.request.Request(base+path+'?verify='+args.sha,headers={'Cache-Control':'no-cache'})
    with urllib.request.urlopen(req,timeout=30) as r:
        return r.read()

for attempt in range(args.attempts):
    try:
        manifest=json.loads(get('manifest.json'))
        info=json.loads(get('build-info.json'))
        if manifest['buildSha']!=args.sha or info['commit']!=args.sha:
            raise ValueError('deployed source commit mismatch')
        builds=manifest['builds']
        if len(builds)!=1 or builds[0]['chipFamily']!='ESP32-S3':
            raise ValueError('wrong target chip')
        parts=builds[0]['parts']
        if len(parts)!=4 or {part['path']:part['offset'] for part in parts}!=expected:
            raise ValueError('unexpected flash image layout')
        html=get('index.html').decode('utf-8')
        if 'esp-web-install-button manifest="manifest.json"' not in html or '今のホイールと重り' not in html:
            raise ValueError('incorrect flash page')
        hashes={name.strip():digest for digest,name in (line.split(maxsplit=1) for line in get('SHA256SUMS.txt').decode().splitlines())}
        verified={}
        for path in expected:
            data=get(path)
            digest=hashlib.sha256(data).hexdigest()
            if not data or hashes.get(path)!=digest:
                raise ValueError('binary checksum mismatch: '+path)
            verified[path]={'bytes':len(data),'sha256':digest}
        print(json.dumps({'url':base,'commit':args.sha,'verified_assets':verified},indent=2))
        break
    except Exception as error:
        print('Publication check',attempt+1,'failed:',error,flush=True)
        if attempt+1==args.attempts:
            raise
        time.sleep(5)
