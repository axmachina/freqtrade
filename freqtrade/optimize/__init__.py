# pragma pylint: disable=missing-docstring

import gzip
try:
    import ujson as json
    _UJSON = True
except ImportError:
    # see mypy/issues/1153
    import json  # type: ignore
    _UJSON = False
import logging
import os
from datetime import datetime
from typing import Optional, List, Dict, Tuple, Any
import operator

import arrow
from pandas import DataFrame

from freqtrade import misc, constants, OperationalException
from freqtrade.exchange import Exchange
from freqtrade.arguments import TimeRange
from freqtrade.optimize.default_hyperopt import DefaultHyperOpts  # noqa: F401

logger = logging.getLogger(__name__)


def json_load(data):
    """Try to load data with ujson"""
    if _UJSON:
        return json.load(data, precise_float=True)
    else:
        return json.load(data)


def trim_tickerlist(tickerlist: List[Dict], timerange: TimeRange) -> List[Dict]:
    if not tickerlist:
        return tickerlist

    start_index = 0
    stop_index = len(tickerlist)

    if timerange.starttype == 'line':
        stop_index = timerange.startts
    if timerange.starttype == 'index':
        start_index = timerange.startts
    elif timerange.starttype == 'date':
        while (start_index < len(tickerlist) and
               tickerlist[start_index][0] < timerange.startts * 1000):
            start_index += 1

    if timerange.stoptype == 'line':
        start_index = len(tickerlist) + timerange.stopts
    if timerange.stoptype == 'index':
        stop_index = timerange.stopts
    elif timerange.stoptype == 'date':
        while (stop_index > 0 and
               tickerlist[stop_index-1][0] > timerange.stopts * 1000):
            stop_index -= 1

    if start_index > stop_index:
        raise ValueError(f'The timerange [{timerange.startts},{timerange.stopts}] is incorrect')

    return tickerlist[start_index:stop_index]


def get_timeframe(data: Dict[str, DataFrame]) -> Tuple[arrow.Arrow, arrow.Arrow]:
    """
    Get the maximum timeframe for the given backtest data
    :param data: dictionary with preprocessed backtesting data
    :return: tuple containing min_date, max_date
    """
    timeframe = [
        (arrow.get(frame['date'].min()), arrow.get(frame['date'].max()))
        for frame in data.values()
    ]
    return min(timeframe, key=operator.itemgetter(0))[0], \
        max(timeframe, key=operator.itemgetter(1))[1]


def validate_backtest_data(data: Dict[str, DataFrame], min_date: datetime,
                           max_date: datetime, ticker_interval_mins: int) -> bool:
    """
    Validates preprocessed backtesting data for missing values and shows warnings about it that.

    :param data: dictionary with preprocessed backtesting data
    :param min_date: start-date of the data
    :param max_date: end-date of the data
    :param ticker_interval_mins: ticker interval in minutes
    """
    # total difference in minutes / interval-minutes
    expected_frames = int((max_date - min_date).total_seconds() // 60 // ticker_interval_mins)
    found_missing = False
    for pair, df in data.items():
        dflen = len(df)
        if dflen < expected_frames:
            found_missing = True
            logger.warning("%s has missing frames: expected %s, got %s, that's %s missing values",
                           pair, expected_frames, dflen, expected_frames - dflen)
    return found_missing


def load_tickerdata_file(
        datadir: str, pair: str,
        ticker_interval: str,
        timerange: Optional[TimeRange] = None) -> Optional[List[Dict]]:
    """
    Load a pair from file,
    :return dict OR empty if unsuccesful
    """
    path = make_testdata_path(datadir)
    pair_s = pair.replace('/', '_')
    file = os.path.join(path, f'{pair_s}-{ticker_interval}.json')
    gzipfile = file + '.gz'

    # If the file does not exist we download it when None is returned.
    # If file exists, read the file, load the json
    if os.path.isfile(gzipfile):
        logger.debug('Loading ticker data from file %s', gzipfile)
        with gzip.open(gzipfile) as tickerdata:
            pairdata = json.load(tickerdata)
    elif os.path.isfile(file):
        logger.debug('Loading ticker data from file %s', file)
        with open(file) as tickerdata:
            pairdata = json.load(tickerdata)
    else:
        return None

    if timerange:
        pairdata = trim_tickerlist(pairdata, timerange)
    return pairdata


def load_data(datadir: str,
              ticker_interval: str,
              pairs: List[str],
              refresh_pairs: Optional[bool] = False,
              exchange: Optional[Exchange] = None,
              timerange: TimeRange = TimeRange(None, None, 0, 0)) -> Dict[str, List]:
    """
    Loads ticker history data for the given parameters
    :return: dict
    """
    result = {}

    # If the user force the refresh of pairs
    if refresh_pairs:
        logger.info('Download data for all pairs and store them in %s', datadir)
        if not exchange:
            raise OperationalException("Exchange needs to be initialized when "
                                       "calling load_data with refresh_pairs=True")
        download_pairs(datadir, exchange, pairs, ticker_interval, timerange=timerange)

    for pair in pairs:
        pairdata = load_tickerdata_file(datadir, pair, ticker_interval, timerange=timerange)
        if pairdata:
            if timerange.starttype == 'date' and pairdata[0][0] > timerange.startts * 1000:
                logger.warning('Missing data at start for pair %s, data starts at %s',
                               pair,
                               arrow.get(pairdata[0][0] // 1000).strftime('%Y-%m-%d %H:%M:%S'))
            if timerange.stoptype == 'date' and pairdata[-1][0] < timerange.stopts * 1000:
                logger.warning('Missing data at end for pair %s, data ends at %s',
                               pair,
                               arrow.get(pairdata[-1][0] // 1000).strftime('%Y-%m-%d %H:%M:%S'))
            result[pair] = pairdata
        else:
            logger.warning(
                'No data for pair: "%s", Interval: %s. '
                'Use --refresh-pairs-cached to download the data',
                pair,
                ticker_interval
            )

    return result


def make_testdata_path(datadir: str) -> str:
    """Return the path where testdata files are stored"""
    return datadir or os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), '..', 'tests', 'testdata'
        )
    )


def download_pairs(datadir, exchange: Exchange, pairs: List[str],
                   ticker_interval: str,
                   timerange: TimeRange = TimeRange(None, None, 0, 0)) -> bool:
    """For each pairs passed in parameters, download the ticker intervals"""
    for pair in pairs:
        try:
            download_backtesting_testdata(datadir,
                                          exchange=exchange,
                                          pair=pair,
                                          tick_interval=ticker_interval,
                                          timerange=timerange)
        except BaseException:
            logger.info(
                'Failed to download the pair: "%s", Interval: %s',
                pair,
                ticker_interval
            )
            return False
    return True


def load_cached_data_for_updating(filename: str,
                                  tick_interval: str,
                                  timerange: Optional[TimeRange]) -> Tuple[
                                                                                  List[Any],
                                                                                  Optional[int]]:
    """
    Load cached data and choose what part of the data should be updated
    """

    since_ms = None

    # user sets timerange, so find the start time
    if timerange:
        if timerange.starttype == 'date':
            since_ms = timerange.startts * 1000
        elif timerange.stoptype == 'line':
            num_minutes = timerange.stopts * constants.TICKER_INTERVAL_MINUTES[tick_interval]
            since_ms = arrow.utcnow().shift(minutes=num_minutes).timestamp * 1000

    # read the cached file
    if os.path.isfile(filename):
        with open(filename, "rt") as file:
            data = json_load(file)
            # remove the last item, because we are not sure if it is correct
            # it could be fetched when the candle was incompleted
            if data:
                data.pop()
    else:
        data = []

    if data:
        if since_ms and since_ms < data[0][0]:
            # the data is requested for earlier period than the cache has
            # so fully redownload all the data
            data = []
        else:
            # a part of the data was already downloaded, so
            # download unexist data only
            since_ms = data[-1][0] + 1

    return (data, since_ms)


def download_backtesting_testdata(datadir: str,
                                  exchange: Exchange,
                                  pair: str,
                                  tick_interval: str = '5m',
                                  timerange: Optional[TimeRange] = None) -> None:

    """
    Download the latest ticker intervals from the exchange for the pair passed in parameters
    The data is downloaded starting from the last correct ticker interval data that
    exists in a cache. If timerange starts earlier than the data in the cache,
    the full data will be redownloaded

    Based on @Rybolov work: https://github.com/rybolov/freqtrade-data
    :param pair: pair to download
    :param tick_interval: ticker interval
    :param timerange: range of time to download
    :return: None

    """
    path = make_testdata_path(datadir)
    filepair = pair.replace("/", "_")
    filename = os.path.join(path, f'{filepair}-{tick_interval}.json')

    logger.info(
        'Download the pair: "%s", Interval: %s',
        pair,
        tick_interval
    )

    data, since_ms = load_cached_data_for_updating(filename, tick_interval, timerange)

    logger.debug("Current Start: %s", misc.format_ms_time(data[1][0]) if data else 'None')
    logger.debug("Current End: %s", misc.format_ms_time(data[-1][0]) if data else 'None')

    # Default since_ms to 30 days if nothing is given
    new_data = exchange.get_history(pair=pair, tick_interval=tick_interval,
                                    since_ms=since_ms if since_ms
                                    else
                                    int(arrow.utcnow().shift(days=-30).float_timestamp) * 1000)
    data.extend(new_data)

    logger.debug("New Start: %s", misc.format_ms_time(data[0][0]))
    logger.debug("New End: %s", misc.format_ms_time(data[-1][0]))

    misc.file_dump_json(filename, data)
