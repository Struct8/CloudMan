import json
import boto3
import csv
import io
import os
import gzip
import re
import urllib.parse
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone

# --- Constantes e Configuração ---
RESOURCE_TAG_KEY = os.environ.get('RESOURCE_TAG_KEY', 'resourceTags/user:Name')
COST_COLUMN = 'lineItem/UnblendedCost'
PRODUCT_COLUMN = 'lineItem/ProductCode'
USAGE_TYPE_COLUMN = 'lineItem/UsageType'
USAGE_START_DATE_COLUMN = 'lineItem/UsageStartDate'

CUR_BUCKET_NAME_FALLBACK = os.environ.get('AWS_S3_BUCKET_NAME_0') or os.environ.get('AWS_S3_BUCKET_TARGET_NAME_0')
CONSOLIDATED_BUCKET_NAME = os.environ.get('AWS_S3_BUCKET_TARGET_NAME_0') or CUR_BUCKET_NAME_FALLBACK
CONSOLIDATED_KEY = os.getenv("CONSOLIDATED_KEY", "consolidated-costs/daily_costs_by_tag.json")
DAYS_TO_RETAIN_ENV = os.getenv("DAYS_TO_RETAIN", "30")

try:
    DAYS_TO_RETAIN = int(DAYS_TO_RETAIN_ENV)
    if DAYS_TO_RETAIN <= 0:
        DAYS_TO_RETAIN = 30
except ValueError:
    DAYS_TO_RETAIN = 30

s3_client = boto3.client('s3')

def decimal_default(obj):
    if isinstance(obj, Decimal): return str(obj)
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")

def load_consolidated_data(bucket, key):
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        print(f"Successfully loaded existing consolidated file from s3://{bucket}/{key}")
        return json.loads(content)
    except s3_client.exceptions.NoSuchKey:
        print(f"Consolidated file not found at s3://{bucket}/{key}. Initializing new structure.")
        return {
            "metadata": {
                "description": f"Daily costs aggregated by tag '{RESOURCE_TAG_KEY}' for the last {DAYS_TO_RETAIN} days.",
                "tag_key_used": RESOURCE_TAG_KEY, 
                "days_retained": DAYS_TO_RETAIN,
                "last_processed_cur_date": None, 
                "last_processed_assembly_id": None,
                "last_updated_timestamp_utc": None, 
                "currency_code": None 
            },
            "costs_by_tag_and_date": {} 
        }
    except Exception as e:
        print(f"ERROR: Failed to load consolidated data from s3://{bucket}/{key}. Error: {e}")
        raise

def save_consolidated_data(bucket, key, data):
    try:
        json_string = json.dumps(data, indent=2, default=decimal_default)
        s3_client.put_object(Bucket=bucket, Key=key, Body=json_string.encode('utf-8'), ContentType='application/json')
        print(f"Successfully saved updated consolidated file to s3://{bucket}/{key}")
    except Exception as e:
        print(f"ERROR: Failed to save consolidated data to s3://{bucket}/{key}. Error: {e}")
        raise

def read_manifest_file(bucket, key):
    try:
        print(f"Reading manifest file: s3://{bucket}/{key}")
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response['Body'].read().decode('utf-8')
        return json.loads(content)
    except Exception as e:
        print(f"ERROR: Failed to read manifest file: {e}")
        raise

def process_single_csv_file(bucket_name, object_key, fallback_date):
    """Processa um único arquivo CSV e retorna os custos agregados."""
    daily_costs_by_tag = {}
    currency_code = None
    body = None
    text_stream = None
    
    print(f"Processing data part: s3://{bucket_name}/{object_key}")
    
    try:
        s3_object = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        body = s3_object['Body']
        is_gzipped = object_key.lower().endswith('.gz')

        if is_gzipped:
            gzip_stream = gzip.GzipFile(fileobj=body)
            text_stream = io.TextIOWrapper(gzip_stream, encoding='utf-8', errors='replace')
        else:
            text_stream = io.TextIOWrapper(body, encoding='utf-8', errors='replace')

        csv_reader = csv.DictReader(text_stream)
        processed_rows = 0
        currency_found = False

        for row in csv_reader:
            processed_rows += 1

            if not currency_found and processed_rows <= 10:
                currency_code_found = row.get('lineItem/CurrencyCode', row.get('pricing/currency'))
                if currency_code_found:
                    currency_code = currency_code_found
                    currency_found = True

            raw_usage_date = row.get(USAGE_START_DATE_COLUMN)
            if raw_usage_date and len(raw_usage_date) >= 10:
                usage_date = raw_usage_date[:10]
            else:
                usage_date = fallback_date or "UnknownDate"

            tag_value = row.get(RESOURCE_TAG_KEY)
            if not tag_value: 
                tag_value = "Untagged"

            cost_str = row.get(COST_COLUMN)
            try:
                cost = Decimal(cost_str) if cost_str else Decimal('0.0')
            except (InvalidOperation, TypeError): 
                cost = Decimal('0.0')
            
            if cost == Decimal('0.0'): 
                continue
            
            product_code = row.get(PRODUCT_COLUMN) or 'UnknownProduct'
            usage_type = row.get(USAGE_TYPE_COLUMN) or 'UnknownUsageType'
            
            if tag_value not in daily_costs_by_tag:
                daily_costs_by_tag[tag_value] = {}
            
            if usage_date not in daily_costs_by_tag[tag_value]:
                daily_costs_by_tag[tag_value][usage_date] = {
                    "TotalUnblendedCost": Decimal('0.0'),
                    "CostsByProduct": {}
                }
            
            daily_costs_by_tag[tag_value][usage_date]['TotalUnblendedCost'] += cost
            
            if product_code not in daily_costs_by_tag[tag_value][usage_date]['CostsByProduct']:
                daily_costs_by_tag[tag_value][usage_date]['CostsByProduct'][product_code] = {}
            
            costs_by_usage = daily_costs_by_tag[tag_value][usage_date]['CostsByProduct'][product_code]
            if usage_type not in costs_by_usage: 
                costs_by_usage[usage_type] = Decimal('0.0')
            costs_by_usage[usage_type] += cost

        print(f"Finished parsing part. Total rows scanned: {processed_rows}.")
        return daily_costs_by_tag, currency_code

    except Exception as e:
        print(f"Error parsing file part {object_key}: {e}")
        return {}, None
    finally:
        if text_stream and not text_stream.closed:
            try: text_stream.close()
            except Exception: pass
        if body and hasattr(body, 'close') and not body.closed:
             try: body.close()
             except Exception: pass

def lambda_handler(event, context):
    print(f"Lambda execution started. Received event: {json.dumps(event)}")

    manifest_key = None
    cur_bucket_name = None

    try:
        if 'Records' in event and isinstance(event['Records'], list) and event['Records'] and 's3' in event['Records'][0]:
            s3_event = event['Records'][0]['s3']
            cur_bucket_name = s3_event['bucket']['name']
            manifest_key = urllib.parse.unquote_plus(s3_event['object']['key'], encoding='utf-8')
        elif isinstance(event, dict) and 'object_key' in event:
            manifest_key = event['object_key']
            if CUR_BUCKET_NAME_FALLBACK:
                cur_bucket_name = CUR_BUCKET_NAME_FALLBACK
            else:
                return {'statusCode': 500, 'body': 'Configuration Error: Fallback bucket env var missing.'}
        else:
            return {'statusCode': 400, 'body': 'Invalid event structure.'}
    except Exception as e:
         return {'statusCode': 400, 'body': f'Error parsing S3 event: {str(e)}'}

    consolidated_bucket = CONSOLIDATED_BUCKET_NAME
    if not consolidated_bucket:
        return {'statusCode': 500, 'body': 'Configuration Error: Target bucket env var missing.'}

    # 1. Carregar arquivo de Manifesto (.json)
    try:
        manifest = read_manifest_file(cur_bucket_name, manifest_key)
    except Exception as e:
        return {'statusCode': 500, 'body': f'Failed to parse manifest: {str(e)}'}

    assembly_id = manifest.get("assemblyId", "UnknownAssembly")
    report_keys = manifest.get("reportKeys", [])

    if not report_keys:
        print("WARNING: No report keys to process inside the manifest.")
        return {'statusCode': 200, 'body': 'No report keys found.'}

    # Extrair data de processamento com base no assemblyId
    processing_date_str = None
    match = re.search(r'(\d{8})T\d{6}Z', assembly_id)
    if match:
        try:
            processing_date_str = datetime.strptime(match.group(1), '%Y%m%d').strftime('%Y-%m-%d')
        except ValueError:
            pass

    # 2. Processar e mesclar em lote os arquivos csv descritos no manifesto
    temp_aggregated_costs = {}
    final_currency_code = None

    for csv_key in report_keys:
        csv_costs, csv_currency = process_single_csv_file(cur_bucket_name, csv_key, processing_date_str)
        if csv_currency:
            final_currency_code = csv_currency

        # Mesclar custos desse arquivo no dicionário temporário do lote
        for tag, dates_dict in csv_costs.items():
            if tag not in temp_aggregated_costs:
                temp_aggregated_costs[tag] = {}
            for d_str, day_data in dates_dict.items():
                if d_str not in temp_aggregated_costs[tag]:
                    temp_aggregated_costs[tag][d_str] = day_data
                else:
                    # Somar TotalUnblendedCost
                    existing_total = temp_aggregated_costs[tag][d_str]["TotalUnblendedCost"]
                    new_total = day_data["TotalUnblendedCost"]
                    temp_aggregated_costs[tag][d_str]["TotalUnblendedCost"] = existing_total + new_total
                    
                    # Mesclar CostsByProduct
                    for prod_code, prod_data in day_data["CostsByProduct"].items():
                        if prod_code not in temp_aggregated_costs[tag][d_str]["CostsByProduct"]:
                            temp_aggregated_costs[tag][d_str]["CostsByProduct"][prod_code] = prod_data
                        else:
                            for usage_type, cost_val in prod_data.items():
                                existing_val = temp_aggregated_costs[tag][d_str]["CostsByProduct"][prod_code].get(usage_type, Decimal('0.0'))
                                temp_aggregated_costs[tag][d_str]["CostsByProduct"][prod_code][usage_type] = existing_val + cost_val

    # Serializar decimais temporários para float/string
    daily_costs_data = json.loads(json.dumps(temp_aggregated_costs, default=decimal_default))

    # 3. Carregar Dados Consolidados Existentes do S3
    try:
        consolidated_data = load_consolidated_data(consolidated_bucket, CONSOLIDATED_KEY)
    except Exception as e:
        return {'statusCode': 500, 'body': f'Failed to load consolidated data: {str(e)}'}

    costs_main_key = 'costs_by_tag_and_date'
    if costs_main_key not in consolidated_data or not isinstance(consolidated_data[costs_main_key], dict):
        consolidated_data[costs_main_key] = {}

    # 4. Mesclar os dados do novo lote com a consolidação histórica
    # Como todos os splits do assembly atual já foram somados em memória no "daily_costs_data",
    # nós podemos atualizar diretamente as datas do relatório consolidado com a certeza de que estão completas.
    updated_tags_count = 0
    for tag_value, dates_dict in daily_costs_data.items():
        if tag_value not in consolidated_data[costs_main_key]:
            consolidated_data[costs_main_key][tag_value] = {}
        for date_str, day_data in dates_dict.items():
            consolidated_data[costs_main_key][tag_value][date_str] = day_data
        updated_tags_count += 1

    print(f"Merged updated date metrics for {updated_tags_count} tags.")

    # 5. Obter a data mais recente no consolidado para balizar o descarte (evita limpar testes antigos)
    all_dates = []
    for tag_val, dates_dict in consolidated_data[costs_main_key].items():
        for d_str in dates_dict.keys():
            try:
                all_dates.append(datetime.strptime(d_str, '%Y-%m-%d').date())
            except ValueError:
                continue

    if all_dates:
        reference_date = max(all_dates)
        print(f"Using latest date found in dataset as reference for retention: {reference_date}")
    else:
        reference_date = datetime.now(timezone.utc).date()
        print(f"Using current date as reference for retention: {reference_date}")

    # Remover (Podar) Dados Antigos
    print(f"Starting pruning of data older than {DAYS_TO_RETAIN} days...")
    cutoff_date = reference_date - timedelta(days=DAYS_TO_RETAIN)
    dates_removed_count = 0
    empty_tags_after_pruning = []

    for tag_value in list(consolidated_data[costs_main_key].keys()):
        dates_to_delete = []
        tag_date_data = consolidated_data[costs_main_key][tag_value]
        
        for date_str in tag_date_data.keys():
            try:
                data_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                if data_date < cutoff_date:
                    dates_to_delete.append(date_str)
            except ValueError:
                continue
                
        for date_to_delete in dates_to_delete:
            del consolidated_data[costs_main_key][tag_value][date_to_delete]
            dates_removed_count += 1
                
        if not consolidated_data[costs_main_key][tag_value]:
            empty_tags_after_pruning.append(tag_value)

    for tag_to_delete in empty_tags_after_pruning:
        del consolidated_data[costs_main_key][tag_to_delete]

    # Atualizar Metadados
    consolidated_data['metadata']['last_processed_cur_date'] = processing_date_str
    consolidated_data['metadata']['last_processed_assembly_id'] = assembly_id
    consolidated_data['metadata']['last_updated_timestamp_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds') + 'Z'
    consolidated_data['metadata']['days_retained'] = DAYS_TO_RETAIN
    if final_currency_code:
        consolidated_data['metadata']['currency_code'] = final_currency_code

    # Salvar o JSON Consolidado Atualizado
    try:
        save_consolidated_data(consolidated_bucket, CONSOLIDATED_KEY, consolidated_data)
    except Exception as e:
        return {'statusCode': 500, 'body': f'Failed to save: {str(e)}'}

    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'Consolidated cost data updated successfully from manifest.',
            'tags_updated': updated_tags_count,
            'old_dates_removed': dates_removed_count
        })
    }