# to do
# chnage email to DL

# pip install requests
# pip install azure-identity
# pip install azure-mgmt-resource
# pip install pandas
# pip install azure-communication-email
# pip install openpyxl
# pip install pip-system-certs
# pip install azure-keyvault-secrets
# pip install azure-keyvault-certificates

# pip install azure-functions

import requests
import csv
import datetime
import calendar
import os
import base64
from azure.identity import DefaultAzureCredential
from azure.identity import CertificateCredential
from azure.mgmt.resource.subscriptions import SubscriptionClient
import pandas as pd
from azure.communication.email import EmailClient
from azure.keyvault.certificates import CertificateClient
from azure.keyvault.secrets import SecretClient
import azure.functions as func
import logging
from azure.appconfiguration.provider import load, AzureAppConfigurationKeyVaultOptions
import json


app_config_endpoint = os.environ["billing_app_config"]
env = os.environ["environment"]

# Generate access token
scope = "https://management.azure.com/.default"

# Securely retrieve secrets from Azure Key Vault
credential = DefaultAzureCredential()


key_vault_options = AzureAppConfigurationKeyVaultOptions(credential=credential)
config = load(endpoint=app_config_endpoint, credential=credential, key_vault_options=key_vault_options)

kv_url = config["BillingApp:kv_url"]
cert_name = config["BillingApp:cert_name"]
client_id = config["BillingApp:billing_app_id"]
tenant_id = config["BillingApp:azure_tenant_id"]
api_version_all = config["BillingApp:api_version"]
parsed_data = json.loads(api_version_all)
api_version = parsed_data[env]
vendor_subscriptions_env = config["BillingApp:vendor_subscriptions"]
vendor_subscriptions = [item.strip().strip('"') for item in vendor_subscriptions_env.split(',')]


certificate_client = CertificateClient(vault_url=kv_url, credential=credential)
certificate = certificate_client.get_certificate(certificate_name=cert_name)
cert_thumbprint = certificate.properties.x509_thumbprint.hex()
client = SecretClient(vault_url=kv_url, credential=credential)

# download pem file from key vault
cert_file = "/tmp/temp.pem"
with open(cert_file, "w") as pem_file:
    pem_file.write(client.get_secret(cert_name).value)

# Authenticate using the service principal certificate
cert_credential = CertificateCredential(
    tenant_id=tenant_id,
    client_id=client_id,
    certificate_path=cert_file
)

os.remove("/tmp/temp.pem")

access_token = cert_credential.get_token(scope).token

# Request headers
headers = {
    'Authorization': f'Bearer {access_token}',
    'Content-Type': 'application/json',
}

# Get user input for the from and to dates
print("This program is going to extract previous month's billing data per RG from all the subscriptions in a tenant.......")


## Set date to previous month

# Get the current date
current_date = datetime.datetime.now()
# Calculate the first and last day of the previous month
first_day_previous_month = (current_date.replace(day=1) - datetime.timedelta(days=1)).replace(day=1)
last_day_previous_month = first_day_previous_month.replace(day=calendar.monthrange(first_day_previous_month.year, first_day_previous_month.month)[1])

# Format the dates as dd-mm-yyyy
from_date_input = first_day_previous_month.strftime('%d-%m-%Y')
to_date_input = last_day_previous_month.strftime('%d-%m-%Y')
# use below lines for local testing
# from_date_input = input("Enter the start of billing date (dd-mm-yyyy): ")
# to_date_input = input("Enter the end billing date (dd-mm-yyyy): ")
# from_date_input="22-07-2024"
# to_date_input="23-07-2024"

# Function to convert date from dd-mm-yyyy to yyyy-mm-dd
def convert_date_format(date_str):
    return datetime.datetime.strptime(date_str, "%d-%m-%Y").strftime("%Y-%m-%d")

# Convert the input dates to the required format
from_date = convert_date_format(from_date_input)
to_date = convert_date_format(to_date_input)

# Append the time part to the user input
from_datetime = f"{from_date}T00:00:00Z"
to_datetime = f"{to_date}T23:59:59Z"

bill_month = from_date_input[-7:]
csv_file_name = f"/tmp/azure-bill-{bill_month}.csv"
xlsx_file_name = f"/tmp/azure-bill-{bill_month}.xlsx"

#Requst body
query = {
    "type": "ActualCost",
    "dataSet": {
        "aggregation": {
            "totalCost": {
                "name": "Cost",
                "function": "Sum"
            }
        },
        "granularity": "none",
        "grouping": [
            {
                "type": "Dimension",
                "name": "ResourceGroupName"
            },
            {
                "type": "Dimension",
                "name": "SubscriptionName"
            }
        ],
        "include": [
            "Tags"
        ]
    },
    "timeframe": "Custom",
    "timePeriod": {
        "from": from_datetime,
        "to": to_datetime
    }
}

# define function app and its scehdule
app = func.FunctionApp()

@app.function_name(name="MonthlyBillingReport")
# schedule="0 45 19 3 * *" or schedule="0 45 19 3 1 2" to run once in every few years
@app.schedule(schedule="0 45 19 3 * *",
              arg_name="MonthlyBillingReport",
              run_on_startup=True)
def main(MonthlyBillingReport: func.TimerRequest) -> None:
    utc_timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    if MonthlyBillingReport.past_due:
        logging.info('The timer is past due!')
    logging.info('Python timer trigger function ran at %s', utc_timestamp)
    extract_billing_data(headers, query, api_version, csv_file_name)
    process_file(csv_file_name, xlsx_file_name)
    sendEmail(csv_file_name, xlsx_file_name)

def extract_billing_data(headers, query, api_version, output_file_csv):
    # Initialize the Subscription client
    subscription_client = SubscriptionClient(cert_credential)
    # Get all subscriptions
    subscriptions = subscription_client.subscriptions.list()

    # Extracting data
    output_data = []

    for subscription in subscriptions:
            subscription_id = subscription.subscription_id
            subscription_name = subscription.display_name

            # Azure Cost Management API endpoint
            query_scope = f'/subscriptions/{subscription_id}'
            endpoint = "https://management.azure.com/"+ query_scope + "/providers/Microsoft.CostManagement/query?" + api_version

            # Send POST request to Azure Cost Management API
            print ("Fetching billing data from subscription..... ", subscription_name)
            response = requests.post(endpoint, headers=headers, json=query)

            #Print response body to console.
            data = response.json()
            # print(type(data))
            # print(data)

            for row in data["properties"]["rows"]:
                cost = float(format(row[0], '.2f'))
                if cost > 0 :
                    resource_group_name = row[1]
                    subscription_name = row[2]
                    management_cost_appox = round(0.0, 2)
                    total_cost = round(cost+management_cost_appox, 2)
                    tags = {tag.split(":")[0].strip('"'): tag.split(":")[1].strip('"') for tag in row[3] if tag.split(":")[1].strip('"') != "environment"}
                    app = tags.get("app", "")
                    environment = tags.get("environment", "")
                    owner = tags.get("owner", "")
                    infrastructure = tags.get("infrastructure", "")
                    if subscription_name in vendor_subscriptions:
                        vendor = "Logicalis"
                    else:
                        vendor = "SA_Managed"
                    output_data.append([app, environment, cost, management_cost_appox, total_cost, owner, resource_group_name, infrastructure, subscription_name, vendor])

    # Write to CSV
    # output_file_csv = "output.csv"
    with open(output_file_csv, 'w', newline='') as csvfile:
        csvwriter = csv.writer(csvfile)
        csvwriter.writerow(["app", "environment", "cost_AUD", "management_cost_appox","total_cost", "owner", "resource_group_name", "infrastructure", "subscription_name", "vendor"])
        csvwriter.writerows(output_data)

    print ("Output data written to file.... ", output_file_csv)


def process_file(input_file, output_file):

    print("Processing file .....")
    print("Reading data from input file....", input_file)
    # Load the CSV file into a DataFrame
    df = pd.read_csv(input_file)

    # Rename 'dwh-shared' to 'data_warehouse' in the 'app' column
    df['app'] = df['app'].replace('dwh-shared', 'data_warehouse')

    # Apply conditions for the 'management_cost_appox' column
    df['management_cost_appox'] = df.apply(lambda row: row['cost_AUD'] * 0.35 if row['infrastructure'] == 'iaas' and row['vendor'] == 'Logicalis'
                                else (row['cost_AUD'] * 0.25 if row['infrastructure'] == 'paas' and row['vendor'] == 'Logicalis'
                                    else (row['cost_AUD'] * 0.3 if row['infrastructure'] == 'mixed' and row['vendor'] == 'Logicalis'
                                        else row['management_cost_appox'])), axis=1)

    # Calculate the 'total_cost' column
    df['total_cost'] = df['cost_AUD'] + df['management_cost_appox']

    # Replace blank app values with "unknown"
    df['app'] = df['app'].fillna('unknown')
    df['app'] = df['app'].replace('', 'unknown')

    # Rename the 'cost_AUD' column to 'Azure_cost'
    df.rename(columns={'cost_AUD': 'azure_cost'}, inplace=True)

    # Group by 'app' and sum the required columns
    result = df.groupby('app').agg({
        'azure_cost': 'sum',
        'management_cost_appox': 'sum',
        'total_cost': 'sum'
    }).reset_index()

    # Select the desired columns
    result = result[['app', 'azure_cost', 'management_cost_appox', 'total_cost']]

    # Write the result to an Excel file
    print("Writing data to output file....", output_file)
    result.to_excel(output_file, index=False)


def sendEmail(attachment_csv, attachment_xlsx):

    connection_string = config["BillingApp:comm_service_conn_string"]

    with open(attachment_csv, "rb") as file:
        file_bytes_b64_csv = base64.b64encode(file.read())

    with open(attachment_xlsx, "rb") as file:
        file_bytes_b64_excel = base64.b64encode(file.read())

    csv_discard_tmp = attachment_csv[5:]
    xlsx_discard_tmp = attachment_xlsx[5:]

    message = {
        "content": {
            "subject": "Azure Monthly Billing Report",
            "plainText": "Attched Azure monthly billing report. CSV file contains raw data, and does not include any third party management costs. Excel file consolidates data to show cost per app, includes approximate management costs charged by vendors. If the management cost is 0, its managed by SA."

        },
        "recipients": {
            "to": [
                {
                    "address": "azure.billing@standards.org.au",
                    "displayName": "Azure Billing"
                },
                {
                    "address": "ash.dey@standards.org.au",
                    "displayName": "Ash Dey"
                }
            ]
        },
        "senderAddress": "DoNotReply@d737b976-8c58-45e0-a0e7-4c16f5c097c4.azurecomm.net",
        "replyTo": [
            {
                "address": "ash.dey@standards.org.au",  # Email address. Required.
                "displayName": "Ash Dey"  # Optional. Email display name.
            }
        ],
        "attachments": [
            {
                "name": csv_discard_tmp,
                "contentType": "text/csv",
                "contentInBase64": file_bytes_b64_csv.decode()
            },
            {
                "name": xlsx_discard_tmp,
                "contentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "contentInBase64": file_bytes_b64_excel.decode()
            }
        ]
    }


    POLLER_WAIT_TIME = 10

    try:
        # endpoint = "https://central-communication-service.australia.communication.azure.com"
        # email_client = EmailClient(endpoint, DefaultAzureCredential())
        print("set email client...")
        email_client = EmailClient.from_connection_string(connection_string)

        print("send email....")
        poller = email_client.begin_send(message);

        time_elapsed = 0
        while not poller.done():
            print("Email send poller status: " + poller.status())

            poller.wait(POLLER_WAIT_TIME)
            time_elapsed += POLLER_WAIT_TIME

            if time_elapsed > 18 * POLLER_WAIT_TIME:
                raise RuntimeError("Polling timed out.")

        if poller.result()["status"] == "Succeeded":
            print(f"Successfully sent the email (operation id: {poller.result()['id']})")
        else:
            raise RuntimeError(str(poller.result()["error"]))

    except Exception as ex:
        print(ex)

'''
# use this part when running locally & comment function app components at the top

bill_month = from_date_input[-7:]
csv_file_name = f"azure-bill-{bill_month}.csv"
xlsx_file_name = f"azure-bill-{bill_month}.xlsx"
extract_billing_data(headers, query, api_version, csv_file_name)
process_file(csv_file_name, xlsx_file_name)
sendEmail(csv_file_name, xlsx_file_name)

'''