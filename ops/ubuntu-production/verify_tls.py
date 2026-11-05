"""Public HTTPS readiness gate. Default expects the deployed app, not maintenance."""
import argparse
import json
import subprocess
import socket
import ssl

parser=argparse.ArgumentParser()
parser.add_argument('--ca-file',default=None,help='Optional trusted CA bundle; never disables certificate verification')
parser.add_argument('--expected-status',type=int,choices=[200,503],default=200)
args=parser.parse_args()
BASE='https://experiment.barsun4818.ir'

def check(url):
    command=['curl','--noproxy','*','--silent','--show-error','--max-time','15',
             '--dump-header','-','--write-out','\nBARSAN_META:%{http_code} %{ssl_verify_result}\n',url]
    if args.ca_file:
        command[1:1]=['--cacert',args.ca_file]
    response=subprocess.run(command,capture_output=True,text=True,timeout=20)
    if response.returncode:
        raise RuntimeError('Public TLS/HTTP request failed')
    headers,tail=response.stdout.rsplit('\nBARSAN_META:',1)
    status,verify=map(int,tail.strip().split())
    head,_,body=headers.partition('\r\n\r\n' if '\r\n' in headers else '\n\n')
    location=next((x.split(':',1)[1].strip() for x in head.splitlines() if x.lower().startswith('location:')),None)
    if url.startswith('https:'):
        assert verify==0,'Certificate chain or hostname validation failed'
    return {'status':status,'certificate_verified':verify==0 if url.startswith('https:') else None,
            'location':location,'body':body}

probe='barsan_tls_probe=synthetic'
http=check('http://experiment.barsun4818.ir/dce/?'+probe)
assert http['status']==308 and http['location']==BASE+'/dce/?'+probe
root=check(BASE+'/?'+probe)
assert root['status']==302 and root['location']==BASE+'/dce/?'+probe
slash=check(BASE+'/dce?'+probe)
assert slash['status']==308 and slash['location']==BASE+'/dce/?'+probe
page=check(BASE+'/dce/')
assert page['status']==args.expected_status
protocols={}
ca_file=args.ca_file
if ca_file is None:
    try:
        import certifi
        ca_file=certifi.where()
    except ImportError:
        pass
for name,version in [('1.2',ssl.TLSVersion.TLSv1_2),('1.3',ssl.TLSVersion.TLSv1_3)]:
    context=ssl.create_default_context(cafile=ca_file)
    context.minimum_version=version
    context.maximum_version=version
    with socket.create_connection(('experiment.barsun4818.ir',443),timeout=10) as connection:
        with context.wrap_socket(connection,server_hostname='experiment.barsun4818.ir') as tls:
            assert tls.version()=='TLSv'+name
            protocols[name]=True
application_ready=False
if args.expected_status==200:
    health=check(BASE+'/dce/api/health')
    data=json.loads(health['body'])
    assert health['status']==200 and data.get('database_connected') is True and data.get('database')=='postgresql'
    application_ready=True
print(json.dumps({'tls_verified':True,'http_redirect_verified':True,'query_preservation_verified':True,
                  'tls_protocols_verified':protocols,'application_status':page['status'],
                  'application_http_database_ready':application_ready}))
