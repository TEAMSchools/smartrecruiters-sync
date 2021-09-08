import json
import os
import pathlib
import time
import traceback
from io import StringIO

import pandas as pd
import requests
from dotenv import load_dotenv
from google.cloud import storage

from datarobot.utilities import email

load_dotenv()

BASE_URL = "https://api.smartrecruiters.com"
SMARTTOKEN = os.getenv("SMARTTOKEN")
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
REPORT_CONFIG_FILE = os.getenv("REPORT_CONFIG_FILE")

PROJECT_PATH = pathlib.Path(__file__).absolute().parent
REPORT_CONFIG_FILEPATH = PROJECT_PATH / REPORT_CONFIG_FILE


def get_all_data(session, url):
    next_page = None
    all_data = []
    while True:
        response = session.get(url, params={"page": next_page})
        response_data = response.json()
        all_data.extend(response_data.get("content"))
        next_page = response_data.get("nextPage")
        if next_page is None:
            break
    return all_data


def main():
    smartrecruiters = requests.Session()
    smartrecruiters.headers["X-SmartToken"] = SMARTTOKEN

    gcs_storage_client = storage.Client()
    gcs_bucket = gcs_storage_client.bucket(GCS_BUCKET_NAME)

    with open(REPORT_CONFIG_FILEPATH, "r") as f:
        report_ids = json.load(f)

    for report_id in report_ids:
        # generate ad-hoc run of report
        print(report_id)
        report_endpoint = f"reporting-api/v201804/reports/{report_id}/files"
        report_url = f"{BASE_URL}/{report_endpoint}"

        try:
            print("\tGenerating ad-hoc report run...")
            generate_response = smartrecruiters.post(report_url)
            generate_response.raise_for_status()
            report_file_status = generate_response.json()["reportFileStatus"]
        except requests.exceptions.HTTPError as e:
            print(f"\t\t{e}")
            print(f"\t\t\t{generate_response.json()['message']}")
            report_file_status = "PENDING"
        except Exception as xc:
            print(xc)
            print(traceback.format_exc())
            email_subject = f"SmartRecruiters Export Error - {report_id}"
            email_body = f"{xc}\n\n{traceback.format_exc()}"
            email.send_email(subject=email_subject, body=email_body)
            continue

        print(f"\t\t{report_file_status}")

        # check report generation status
        print("\tChecking report status...")
        while report_file_status != "COMPLETED":
            status_response_data = get_all_data(smartrecruiters, report_url)

            status_response_data.sort(key=lambda d: d["schedulingDate"])
            report_file_status = status_response_data[-1]["reportFileStatus"]
            print(f"\t\t{report_file_status}")

            if report_file_status == "COMPLETED":
                break
            else:
                time.sleep(0.1)  # rate-limit 10 req/sec

        # retrieve report data
        print("\tDownloading report...")
        download_url = f"{report_url}/recent/data"
        download_response = smartrecruiters.get(download_url)

        # clean up column headers
        print("\tCleaning up column headers...")
        df = pd.read_csv(StringIO(download_response.text))
        for i, val in enumerate(df.columns.values):
            val_clean = (
                val.replace("?", "")
                .replace("(", "")
                .replace(")", "")
                .replace("Screening Question Answer: ", "")
                .replace(": ", "_")
                .replace("| ", "_")
                .replace(", ", "_")
                .replace(" ", "_")
                .replace("-", "_")
                .replace("/", "_")
                .replace("___", "_")
                .replace("__", "_")
                .replace("National", "kf")
                .replace("New_Jersey_Miami", "taf")
                .replace("New_Jersey", "nj")
                .replace("Miami", "mia")
                .strip()
                .lower()
            )
            df.columns.values[i] = val_clean

        # save file
        print("\tSaving file...")
        data_path = PROJECT_PATH / "data" / report_id
        if not data_path.exists():
            data_path.mkdir(parents=True)
            print(f"\tCreated {data_path}...")

        data_filepath = data_path / f"{report_id}.csv"
        df.to_csv(data_filepath, index=False)

        # upload to GCS
        destination_blob_name = "smartrecruiters/" + "/".join(data_filepath.parts[-2:])

        print(f"\tUploading to {destination_blob_name}...")
        blob = gcs_bucket.blob(destination_blob_name)
        blob.upload_from_filename(data_filepath)


if __name__ == "__main__":
    try:
        main()
    except Exception as xc:
        email_subject = "SmartRecruiters Export Error"
        email_body = f"{xc}\n\n{traceback.format_exc()}"
        email.send_email(subject=email_subject, body=email_body)
