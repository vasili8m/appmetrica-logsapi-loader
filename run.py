#!/usr/bin/env python
"""
  logs_api_int_script.py

  This file is a part of the AppMetrica.

  Copyright 2017 YANDEX

  You may not use this file except in compliance with the License.
  You may obtain a copy of the License at:
        https://yandex.com/legal/metrica_termsofuse/
"""

import datetime
import json
import time
from typing import Tuple

import requests
import logging
import pandas as pd
import io

import settings

logger = logging.getLogger(__name__)


def setup_logging(debug: bool = False) -> None:
    logging_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    level = logging.INFO
    if debug:
        level = logging.DEBUG
    logging.basicConfig(format=logging_format, level=level)


def get_create_date(api_key: str, token: str) -> str:
    url_tmpl = 'https://api.appmetrica.yandex.ru/management/v1/application' \
               '/{id}' \
               '?oauth_token={token}'
    url = url_tmpl.format(
        id=api_key,
        token=token
    )
    r = requests.get(url)
    create_date = None
    if r.status_code == 200:
        app_details = json.load(r.text)
        if ('application' in app_details) \
                and ('create_date' in app_details['application']):
            create_date = app_details['application']['create_date']
    return create_date


def load_logs_api_data(api_key: str, date1: str, date2: str,
                       token: str) -> pd.DataFrame:
    fields = '%2C'.join([
        'event_name',
        'event_timestamp',
        'appmetrica_device_id',
        'os_name',
        'country_iso_code',
        'city',
    ])
    url_tmpl = 'https://api.appmetrica.yandex.ru/logs/v1/export/events.csv' \
               '?application_id={api_key}' \
               '&date_since={date1}%2000%3A00%3A00' \
               '&date_until={date2}%2023%3A59%3A59' \
               '&date_dimension=default' \
               '&fields={fields}' \
               '&oauth_token={token}'
    url = url_tmpl.format(api_key=api_key,
                          date1=date1,
                          date2=date2,
                          fields=fields,
                          token=token)

    r = requests.get(url)

    while r.status_code != 200:
        logger.debug('Logs API response code: {}'.format(r.status_code))
        if r.status_code != 202:
            raise ValueError(r.text)

        time.sleep(10)
        r = requests.get(url)

    df = pd.read_csv(io.StringIO(r.text))
    df['event_date'] = list(map(lambda x: datetime.datetime
                                .fromtimestamp(x)
                                .strftime('%Y-%m-%d'), df.event_timestamp))
    return df


def get_clickhouse_auth() -> Tuple[str, str]:
    auth = None
    if settings.CH_USER:
        auth = (settings.CH_USER, settings.CH_PASSWORD)
    return auth


def query_clickhouse(data: str, params: dict = None) -> str:
    """Returns ClickHouse response"""
    log_data = data
    if len(log_data) > 200:
        log_data = log_data[:200] + '[...]'
    logger.debug('Query ClickHouse:\n{}\n\tHTTP params: {}'
                 .format(log_data, params))
    host = settings.CH_HOST
    auth = get_clickhouse_auth()
    r = requests.post(host, data=data, params=params, auth=auth)
    if r.status_code == 200:
        return r.text
    else:
        raise ValueError(r.text)


def get_clickhouse_data(query: str) -> str:
    """Returns ClickHouse response"""
    return query_clickhouse(query)


def upload_clickhouse_data(db: str, table: str, content: str) -> str:
    """Uploads data to table in ClickHouse"""
    query = 'INSERT INTO {db}.{table} FORMAT TabSeparatedWithNames' \
        .format(db=db, table=table)
    return query_clickhouse(content, params={'query': query})


def drop_table(db: str, table: str) -> None:
    q = 'DROP TABLE IF EXISTS {db}.{table}'.format(
        db=db,
        table=table
    )
    get_clickhouse_data(q)


def database_exists(db: str) -> bool:
    q = 'SHOW DATABASES'
    dbs = get_clickhouse_data(q).strip().split('\n')
    return db in dbs


def database_create(db: str) -> None:
    q = 'CREATE DATABASE {db}'.format(db=db)
    get_clickhouse_data(q)


def table_exists(db: str, table: str) -> bool:
    q = 'SHOW TABLES FROM {db}'.format(db=db)
    tables = get_clickhouse_data(q).strip().split('\n')
    return table in tables


def table_create(db: str, table: str) -> None:
    q = '''
    CREATE TABLE {db}.{table} (
        EventDate Date,
        DeviceID String,
        EventName String,
        EventTimestamp UInt64,
        AppPlatform String,
        Country String,
        APIKey UInt64
    )
    ENGINE = MergeTree(EventDate, 
                        cityHash64(DeviceID), 
                        (EventDate, cityHash64(DeviceID)), 
                        8192)
    '''.format(
        db=db,
        table=table
    )
    get_clickhouse_data(q)


def create_tmp_table_for_insert(db: str, table: str, date1: str, date2: str,
                                tmp_table: str, tmp_data_ins: str) -> None:
    q = '''
        CREATE TABLE {db}.{tmp_data_ins} ENGINE = MergeTree(EventDate, 
                                            cityHash64(DeviceID),
                                            (EventDate, cityHash64(DeviceID)), 
                                            8192)
        AS
        SELECT
            EventDate,
            DeviceID,
            EventName,
            EventTimestamp,
            AppPlatform,
            Country,
            APIKey
        FROM {db}.{tmp_table}
        WHERE NOT ((EventDate, 
                    DeviceID,
                    EventName,
                    EventTimestamp,
                    AppPlatform,
                    Country,
                    APIKey) 
            GLOBAL IN (SELECT
                EventDate,
                DeviceID,
                EventName,
                EventTimestamp,
                AppPlatform,
                Country,
                APIKey
            FROM {db}.{table}
            WHERE EventDate >= '{date1}' AND EventDate <= '{date2}'))
    '''.format(
        db=db,
        tmp_table=tmp_table,
        tmp_data_ins=tmp_data_ins,
        table=table,
        date1=date1,
        date2=date2
    )

    get_clickhouse_data(q)


def insert_data_to_prod(db: str, from_table: str, to_table: str) -> None:
    q = '''
        INSERT INTO {db}.{to_table}
            SELECT
                EventDate,
                DeviceID,
                EventName,
                EventTimestamp,
                AppPlatform,
                Country,
                APIKey
            FROM {db}.{from_table}
    '''.format(
        db=db,
        from_table=from_table,
        to_table=to_table
    )

    get_clickhouse_data(q)


def process_date(date: str, token: str, api_key: str,
                 db: str, table: str) -> None:
    df = load_logs_api_data(api_key, date, date, token)
    df = df.drop_duplicates()
    df['api_key'] = api_key

    temp_table = '{}_tmp_data'.format(table)
    temp_table_insert = '{}_tmp_data_ins'.format(table)

    drop_table(db, temp_table)
    drop_table(db, temp_table_insert)

    table_create(db, temp_table)

    upload_clickhouse_data(
        db,
        temp_table,
        df[['event_date',
            'appmetrica_device_id',
            'event_name',
            'event_timestamp',
            'os_name',
            'country_iso_code',
            'api_key']].to_csv(index=False, sep='\t')
    )
    create_tmp_table_for_insert(db, table, date, date,
                                temp_table, temp_table_insert)
    insert_data_to_prod(db, temp_table_insert, table)

    drop_table(db, 'tmp_data')
    drop_table(db, 'tmp_data_ins')


def update(first_flag: bool = False) -> None:
    days_delta = 7
    if first_flag:
        days_delta = settings.HISTORY_PERIOD

    time_delta = datetime.timedelta(days_delta)
    today = datetime.datetime.today()
    date1 = (today - time_delta).strftime('%Y-%m-%d')
    date2 = today.strftime('%Y-%m-%d')

    database = settings.CH_DATABASE
    if not database_exists(database):
        database_create(database)
        logger.info('Database "{}" created'.format(database))

    table = settings.CH_TABLE
    if not table_exists(database, table):
        table_create(database, table)
        logger.info('Table "{}" created'.format(table))

    logger.info('Loading period {} - {}'.format(date1, date2))
    token = settings.TOKEN
    api_keys = settings.API_KEYS
    for api_key in api_keys:
        logger.info('Processing API key: {}'.format(api_key))
        for date in pd.date_range(date1, date2):
            date_str = date.strftime('%Y-%m-%d')
            logger.info('Loading data for {}'.format(date_str))
            process_date(date_str, token, api_key, database, table)
    logger.info('Finished loading data')


def main():
    setup_logging(settings.DEBUG)

    is_first = True
    logger.info("Starting updater loop "
                "(interval = {} seconds)".format(settings.FETCH_INTERVAL))
    while True:
        try:
            if is_first:
                logger.info('Loading historical data')
                update(first_flag=True)
                is_first = False
            else:
                logger.info("Run Logs API fetch")
                update(first_flag=False)
            time.sleep(settings.FETCH_INTERVAL)
        except KeyboardInterrupt:
            return


if __name__ == '__main__':
    main()
