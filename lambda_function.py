from base64 import b64encode, b64decode
from hashlib import sha256
import hmac
import json
import os

import twitter
import reverse_geocoder as rg
from motionless import DecoratedMap, LatLonMarker

ssm = boto3.client('ssm', region_name='us-east-1')
sagemaker = boto3.client('runtime.sagemaker')
ENDPOINT_NAME = os.getenv("SAGEMAKER_ENDPOINT_NAME", "predictv2")
SSM_CREDS_NAME = os.getenv("SSM_CREDS_NAME", "/twitter/whereml")
GMAPS_API_KEY = os.getenv("GMAPS_API_KEY", "/google/mapsapi")
MAX_PREDICTIONS = os.getenv("MAX_PREDICTIONS", 3)
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/twitter/whereml")
CREDS = ssm.get_parameter(Name=SSM_CREDS_NAME)['Parameter']['Value'].split(',')
CONSUMER_SECRET = CREDS[1]
twitter_api = twitter.Api(*CREDS)
del CREDS
TWITTER_SN = twitter_api.VerifyCredentials().screen_name

def sign_crc(crc):
    h = hmac.new(bytes(CONSUMER_SECRET, 'ascii'), bytes(crc, 'ascii'), digestmod=sha256)
    return json.dumps({
        "response_token": "sha256="+b64encode(h.digest()).decode()
    })

def verify_request(event, context):
    crc = event['headers']['X-Twitter-Webhooks-Signature']
    h = hmac.new(bytes(CONSUMER_SECRET, 'ascii'), bytes(event['body'], 'utf-8'), digestmod=sha256)
    crc = b64decode(crc[7:]) # strip out the first 7 characters
    return hmac.compare_digest(h.digest(), crc)

def validate_record(event):
    sn = str(event.get('in_reply_to_screen_name')).lower == TWITTER_SN
    media = event.get('entities',{}).get('media',[{}])[0].get('media_url')
    if and(
        TWITTER_SN.lower() in event.get('text', '').lower()
        sn,
        media
    ):
        return True
    return False


def unicode_flag(code):
    code = code.upper()
    OFFSET = ord('🇦') - ord('A')
    return chr(ord(code[0]) + OFFSET) + chr(ord(code[1]) + OFFSET)


def build_tweet(results):
    status = []
    dmap = DecoratedMap(size_x=640, size_y=320, key=GMAPS_API_KEY)
    for result in rg.search([tuple(res) for res in results]):
        status.append(", ".join([result['name'], result['admin1'], unicode_flag(result['cc'])]))
    for result in results:
        dmap.add_marker(LatLonMarker(result[0], result[1]))
    img_url = dmap.generate_url()
    return '\n'.join(status), img_url
    

def lambda_handler(events, context):
    
    # deal with bad requests
    if event.get('path') != WEBHOOK_PATH:
        return {
            'statusCode': 404,
            'body': ''
        }
    
    # deal with subscription calls
    if event.get('httpMethod') == 'GET':
        crc = event.get('queryStringParameters', {}).get('crc_token')
        if not crc:
            return {
                'statusCode': 401,
                'body': 'bad crc'
            }
        return {
            'statusCode': 200,
            'body': sign_crc(crc)
        }
    
    # deal with bad crc
    if not verify_request(event, context):
        print("Unable to verify CRC")
        return {
            'statusCode': 400,
            'body': 'bad crc'
        }
    
    
    twitter_events = json.loads(event['body'])
    for event in twitter_events.get('tweet_create_events', []):
        if validate_record(event):
            results = json.loads(sagemaker.invoke_endpoint(
                EndpointName=ENDPOINT_NAME,
                Body=json.dumps({
                        'url': media,
                        'max_predictions': MAX_PREDICTIONS
                    })
            )['Body'].read())
            status = build_tweet(results)
            twitter_api.PostUpdate(
                "📍 ?\n" + status[0],
                media=status[1],
                in_reply_to_status_id=event['id_str'],
                auto_populate_reply_metadata=True
            )