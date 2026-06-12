import json
import boto3
import csv
import io
import os
import gzip
import re
import urllib.parse
from decimal import Decimal, getcontext, InvalidOperation
from datetime import datetime, timedelta, timezone

# --- Constantes e Configuração ---
# Chave da tag a ser usada para agrupar os custos
RESOURCE_TAG_KEY = os.environ.get('RESOURCE_TAG_KEY', 'resourceTags/user:Name')

# Colunas padrão do CUR a serem usadas
COST_COLUMN = 'lineItem/UnblendedCost'
PRODUCT_COLUMN = 'lineItem/ProductCode'
USAGE_TYPE_COLUMN = 'lineItem/UsageType'

# Usamos .get() para não quebrar a inicialização da Lambda caso a variável não exista
CUR_BUCKET_NAME_FALLBACK = os.environ.get('AWS_S3_BUCKET_NAME_0')

# Variável de destino do arquivo consolidado (se não houver TARGET, tenta usar o mesmo fallback)
CONSOLIDATED_BUCKET_NAME = os.environ.get('AWS_S3_BUCKET_TARGET_NAME_0', CUR_BUCKET_NAME_FALLBACK)

# --- Configuração do Arquivo JSON Consolidado ---
CONSOLIDATED_KEY = os.getenv("CONSOLIDATED_KEY", "consolidated-costs/daily_costs_by_tag.json")
DAYS_TO_RETAIN_ENV = os.getenv("DAYS_TO_RETAIN", "30")

try:
    DAYS_TO_RETAIN = int(DAYS_TO_RETAIN_ENV)
    if DAYS_TO_RETAIN <= 0:
        print(f"Warning: DAYS_TO_RETAIN ({DAYS_TO_RETAIN_ENV}) must be positive. Defaulting to 30.")
        DAYS_TO_RETAIN = 30
except ValueError:
    print(f"Warning: Invalid value for DAYS_TO_RETAIN ('{DAYS_TO_RETAIN_ENV}'). Defaulting to 30.")
    DAYS_TO_RETAIN = 30

s3_client = boto3.client('s3')

# --- Funções Helper ---

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
                "tag_key_used": RESOURCE_TAG_KEY, "days_retained": DAYS_TO_RETAIN,
                "last_processed_cur_date": None, "last_updated_timestamp_utc": None, "currency_code": None },
            "costs_by_tag_and_date": {} }
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to decode JSON from s3://{bucket}/{key}. Error: {e}")
        raise ValueError(f"Invalid JSON content in consolidated file: {key}") from e
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

# --- Lógica de Processamento do CUR ---

def process_cur_file(bucket_name, object_key):
    print("process_cur_file object_key:", object_key)
    """
    Processa um único arquivo CUR. Extrai a data 'YYYY-MM-DD' OBRIGATORIAMENTE
    do caminho do objeto S3 (object_key) procurando por 'YYYYMMDDTHHMMSSZ'.
    """
    daily_costs_by_tag = {}
    processing_date_str = None
    currency_code = None
    body = None
    text_stream = None
    
    print(f"Attempting to process CUR file: s3://{bucket_name}/{object_key}")
    
    # --- PASSO 1: Extrair data ---
    print(f"Attempting to extract date from object key: {object_key}")
    match = re.search(r'/(\d{8})T\d{6}Z/', '/' + object_key + '/')
    if match:
        date_yyyymmdd = match.group(1)
        try:
            date_obj = datetime.strptime(date_yyyymmdd, '%Y%m%d')
            processing_date_str = date_obj.strftime('%Y-%m-%d')
            print(f"Successfully extracted date from object key: {processing_date_str}")
        except ValueError:
            print(f"ERROR: Found potential date '{date_yyyymmdd}' in key, but failed to parse it.")
            return None, {}, None
    else:
        print(f"ERROR: Could not find required date pattern (YYYYMMDDTHHMMSSZ) in object key: {object_key}")
        return None, {}, None

    # --- PASSO 2: Processar o Arquivo CSV ---
    try:
        s3_object = s3_client.get_object(Bucket=bucket_name, Key=object_key)
        body = s3_object['Body']
        print("S3 object retrieved. Detecting compression and processing stream...")
        is_gzipped = object_key.lower().endswith('.gz')

        if is_gzipped:
            gzip_stream = gzip.GzipFile(fileobj=body)
            text_stream = io.TextIOWrapper(gzip_stream, encoding='utf-8', errors='replace')
        else:
            text_stream = io.TextIOWrapper(body, encoding='utf-8', errors='replace')

        csv_reader = csv.DictReader(text_stream)
        print("CSV stream configured. Processing rows for costs and currency...")
        processed_rows = 0
        currency_found = False

        for row_num, row in enumerate(csv_reader):
            processed_rows += 1

            if not currency_found and processed_rows <= 10:
                currency_code_found = row.get('lineItem/CurrencyCode', row.get('pricing/currency'))
                if currency_code_found:
                    currency_code = currency_code_found
                    currency_found = True
                    print(f"Determined currency code: {currency_code}")

            tag_value = row.get(RESOURCE_TAG_KEY)
            if not tag_value: tag_value = "Untagged"
            cost_str = row.get(COST_COLUMN)
            try:
                cost = Decimal(cost_str) if cost_str else Decimal('0.0')
            except (InvalidOperation, TypeError): cost = Decimal('0.0')
            if cost == Decimal('0.0'): continue
            
            product_code = row.get(PRODUCT_COLUMN) or 'UnknownProduct'
            usage_type = row.get(USAGE_TYPE_COLUMN) or 'UnknownUsageType'
            
            if tag_value not in daily_costs_by_tag:
                daily_costs_by_tag[tag_value] = {"TotalUnblendedCost": Decimal('0.0'), "CostsByProduct": {}}
            daily_costs_by_tag[tag_value]['TotalUnblendedCost'] += cost
            
            if product_code not in daily_costs_by_tag[tag_value]['CostsByProduct']:
                daily_costs_by_tag[tag_value]['CostsByProduct'][product_code] = {}
            costs_by_usage = daily_costs_by_tag[tag_value]['CostsByProduct'][product_code]
            if usage_type not in costs_by_usage: costs_by_usage[usage_type] = Decimal('0.0')
            costs_by_usage[usage_type] += cost

        print(f"Finished processing CUR file content. Total rows scanned: {processed_rows}.")

        if processed_rows == 0:
            print("Warning: CUR file was empty, but date was extracted from key. Proceeding with potentially zero costs.")

        final_daily_costs_dict = json.loads(json.dumps(daily_costs_by_tag, default=decimal_default))

        print(f"Aggregation complete for date {processing_date_str}. Found {len(final_daily_costs_dict)} tags with costs.")
        return processing_date_str, final_daily_costs_dict, currency_code

    except s3_client.exceptions.NoSuchKey:
        print(f"Error: CUR file not found - s3://{bucket_name}/{object_key}")
        return None, {}, None
    except gzip.BadGzipFile:
         print(f"Fatal: File s3://{bucket_name}/{object_key} is not a valid Gzip file.")
         return None, {}, None
    except UnicodeDecodeError as e:
        print(f"Fatal: Could not decode file s3://{bucket_name}/{object_key} as UTF-8. Error: {e}")
        return None, {}, None
    except Exception as e:
        print(f"An unexpected error occurred during CUR file content processing for s3://{bucket_name}/{object_key}: {e}")
        import traceback
        traceback.print_exc()
        return None, {}, None
    finally:
        if text_stream and not text_stream.closed:
            try: text_stream.close()
            except Exception as close_err: print(f"Warning: Error closing text stream: {close_err}")
        if body and hasattr(body, 'close') and not body.closed:
             try: body.close()
             except Exception as close_err: print(f"Warning: Error closing S3 body stream: {close_err}")


# --- Função Lambda Handler Principal ---

def lambda_handler(event, context):
    """ Ponto de entrada da Lambda """
    print(f"Lambda execution started. Received event: {json.dumps(event)}")

    cur_object_key = None
    cur_bucket_name = None

    # --- 1. Determinar Bucket/Chave do Arquivo CUR de Entrada ---
    try:
        # Tenta extrair do evento de notificação do S3
        if 'Records' in event and isinstance(event['Records'], list) and event['Records'] and isinstance(event['Records'][0], dict) and 's3' in event['Records'][0]:
            s3_event = event['Records'][0]['s3']
            cur_bucket_name = s3_event['bucket']['name']
            cur_object_key = urllib.parse.unquote_plus(s3_event['object']['key'], encoding='utf-8')
            print(f"Triggered by S3 event. Processing CUR file: s3://{cur_bucket_name}/{cur_object_key}")
        
        # Invocaçao manual (como por exemplo via API/Teste enviando um "object_key")
        elif isinstance(event, dict) and 'object_key' in event:
            cur_object_key = event['object_key']
            print("cur_object_key:", cur_object_key)
            
            # Aqui usamos o Fallback seguro caso o bucket não venha no evento da chamada direta
            if CUR_BUCKET_NAME_FALLBACK:
                cur_bucket_name = CUR_BUCKET_NAME_FALLBACK
                print(f"Triggered by direct invocation. object_key='{cur_object_key}'. Using CUR bucket from env var: {cur_bucket_name}")
            else:
                print("CRITICAL ERROR: Env var AWS_S3_BUCKET_NAME_0 is required for CUR bucket in direct invocation but is not set!")
                return {'statusCode': 500, 'body': 'Configuration Error: AWS_S3_BUCKET_NAME_0 missing for direct invocation fallback.'}
        
        else:
            print(f"ERROR: Invalid event structure. Cannot determine CUR file source. Event dump: {json.dumps(event)}")
            return {'statusCode': 400, 'body': 'Invalid event structure for CUR file source.'}
            
    except (KeyError, IndexError, TypeError) as e:
         print(f"ERROR: Could not parse expected S3 event details. Error: {e}")
         return {'statusCode': 400, 'body': 'Error parsing S3 event structure.'}

    if not cur_bucket_name or not cur_object_key:
         print("ERROR: Failed to determine CUR bucket name or clean object key after checking event types.")
         return {'statusCode': 400, 'body': 'Could not determine CUR S3 bucket or key.'}

    # --- 2. Determinar Bucket/Chave do JSON CONSOLIDADO (Saída) ---
    consolidated_bucket = CONSOLIDATED_BUCKET_NAME
    if not consolidated_bucket:
        print("CRITICAL ERROR: Environment variable for consolidated file target is not set!")
        return {'statusCode': 500, 'body': 'Configuration Error: Target bucket env var missing.'}

    consolidated_key = CONSOLIDATED_KEY
    print(f"Consolidated JSON target location: s3://{consolidated_bucket}/{consolidated_key}")
    print(f"Configured tag key for aggregation: {RESOURCE_TAG_KEY}")
    print(f"Data retention period set to: {DAYS_TO_RETAIN} days")

    # --- 3. Processar o Arquivo CUR de Entrada ---
    # Passa o bucket resolvido e a chave limpa para process_cur_file
    processing_date_str, daily_costs_data, currency_code = process_cur_file(cur_bucket_name, cur_object_key)

    if not processing_date_str or daily_costs_data is None:
        print("ERROR: Failed to process CUR file (likely date extraction from key failed or file access error). Aborting update.")
        return {'statusCode': 200, 'body': 'Failed to process CUR file; no update performed.'}

    print(f"Successfully processed CUR for date: {processing_date_str}. Currency: {currency_code or 'Not Found'}.")

    # --- 4. Carregar Dados Consolidados Existentes ---
    try:
        consolidated_data = load_consolidated_data(consolidated_bucket, consolidated_key)
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to load or initialize consolidated data file s3://{consolidated_bucket}/{consolidated_key}. Error: {e}")
        return {'statusCode': 500, 'body': f'Failed to load/initialize consolidated data: {str(e)}'}

    # --- 5. Mesclar Dados do Novo Dia no JSON Consolidado ---
    costs_main_key = 'costs_by_tag_and_date'
    if costs_main_key not in consolidated_data or not isinstance(consolidated_data[costs_main_key], dict):
        consolidated_data[costs_main_key] = {}

    updated_tags_count = 0
    for tag_value, day_data in daily_costs_data.items():
        if tag_value not in consolidated_data[costs_main_key]:
            consolidated_data[costs_main_key][tag_value] = {}
        consolidated_data[costs_main_key][tag_value][processing_date_str] = day_data
        updated_tags_count += 1
    print(f"Merged data for {updated_tags_count} tags for date {processing_date_str}.")

    # --- 6. Remover (Podar) Dados Antigos ---
    print(f"Starting pruning of data older than {DAYS_TO_RETAIN} days...")
    cutoff_date = datetime.now(timezone.utc).date() - timedelta(days=DAYS_TO_RETAIN)
    print(f"Cutoff date (exclusive): {cutoff_date.strftime('%Y-%m-%d')}")
    dates_removed_count = 0
    empty_tags_after_pruning = []

    for tag_value in list(consolidated_data[costs_main_key].keys()):
        if tag_value not in consolidated_data[costs_main_key]: continue
        dates_to_delete_for_this_tag = []
        tag_date_data = consolidated_data[costs_main_key][tag_value]
        
        for date_str in tag_date_data.keys():
            try:
                data_date = datetime.strptime(date_str, '%Y-%m-%d').date()
                if data_date < cutoff_date:
                    dates_to_delete_for_this_tag.append(date_str)
            except ValueError:
                print(f"Warning: Invalid date format key '{date_str}' under tag '{tag_value}'. Skipping.")
                continue
                
        if dates_to_delete_for_this_tag:
            print(f"  Pruning tag '{tag_value}': Removing dates {dates_to_delete_for_this_tag}")
            for date_to_delete in dates_to_delete_for_this_tag:
                del consolidated_data[costs_main_key][tag_value][date_to_delete]
                dates_removed_count += 1
                
        if not consolidated_data[costs_main_key][tag_value]:
            empty_tags_after_pruning.append(tag_value)

    if empty_tags_after_pruning:
        print(f"Removing {len(empty_tags_after_pruning)} tags that became empty after pruning: {empty_tags_after_pruning}")
        for tag_to_delete in empty_tags_after_pruning:
            del consolidated_data[costs_main_key][tag_to_delete]
            
    print(f"Pruning complete. Total old date entries removed: {dates_removed_count}. Empty tags removed: {len(empty_tags_after_pruning)}.")

    # --- 7. Atualizar Metadados ---
    consolidated_data['metadata']['last_processed_cur_date'] = processing_date_str
    consolidated_data['metadata']['last_updated_timestamp_utc'] = datetime.now(timezone.utc).isoformat(timespec='seconds') + 'Z'
    consolidated_data['metadata']['days_retained'] = DAYS_TO_RETAIN
    if currency_code and not consolidated_data['metadata'].get('currency_code'):
        consolidated_data['metadata']['currency_code'] = currency_code
        print(f"Updated metadata currency code to: {currency_code}")

    # --- 8. Salvar o JSON Consolidado Atualizado ---
    try:
        save_consolidated_data(consolidated_bucket, consolidated_key, consolidated_data)
    except Exception as e:
        print(f"CRITICAL ERROR: Failed to save updated consolidated data to s3://{consolidated_bucket}/{consolidated_key}. Error: {e}")
        return {'statusCode': 500, 'body': f'Failed to save updated consolidated data: {str(e)}'}

    # --- Fim da Execução ---
    print("Lambda execution finished successfully.")
    return {
        'statusCode': 200,
        'body': json.dumps({
            'message': 'Consolidated cost data updated successfully.',
            'processed_cur_file': f's3://{cur_bucket_name}/{cur_object_key}',
            'processed_date': processing_date_str,
            'consolidated_file': f's3://{consolidated_bucket}/{consolidated_key}',
            'tags_updated_for_date': updated_tags_count,
            'old_dates_removed': dates_removed_count,
            'empty_tags_removed': len(empty_tags_after_pruning)
        })
    }