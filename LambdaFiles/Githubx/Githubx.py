import json
#08/03/25 -109
def lambda_handler(event, context):
    return {
        'statusCode': 200,
        'body': json.dumps('Hello from CloudMan (Python)!')
    }
