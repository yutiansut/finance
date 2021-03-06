"""
Get a full listing symbols.

Source provider is http://www.nasdaq.com/screening/company-list.aspx

"""

import concurrent.futures
import logging
import re
import shutil
import numpy as np

import pandas
import pandas_datareader.data as web

from stock.stock_market import StockMarket
from utility.utility import *


class UsaMarket(StockMarket):
    def __init__(self,
                 provider_url=Utility.get_config(Market.US).stock_list_provider,
                 exchanges=Utility.get_config(Market.US).exchanges,
                 concurrent=Utility.get_config(Market.US).concurrent,
                 retry=Utility.get_config().data_retry):
        super(UsaMarket, self).__init__(Market.US)
        self.logger = logging.getLogger(__name__)
        self.provider_url = provider_url
        self.exchanges = exchanges
        self.concurrent = concurrent
        self.retry = retry

    def refresh_listing(self, excel_file=Utility.get_stock_listing_xlsx(Market.US)):
        """
        refresh symbols lists from source provider, and store in sto
        
        :param excel_file: file name to the stored file, actual file name will append _yyyy-mm-dd.xlsx
        :return: void
        """
        try:
            with pandas.ExcelWriter(excel_file) as writer:
                self.logger.info('Saving stock listings to %s.', excel_file)
                for exchange in self.exchanges:
                    try:
                        listings = pandas.read_csv(self.provider_url % exchange)
                        # normalize the listing fields
                        listings.rename(columns={'Symbol': ListingField.Symbol.value,
                                                 'IPOyear': ListingField.IPO.value,
                                                 'Sector': ListingField.Sector.value,
                                                 'industry': ListingField.Industry.value,
                                                 }, index=str, inplace=True)
                        listings.to_excel(writer, exchange)
                        self.logger.info('Saved stock listings for exchange %s.', exchange.upper())
                    except Exception as e:
                        self.logger.error('Fetching stock listings error for exchange %s: %s', exchange.upper(), e)
        except Exception as e:
            self.logger.exception('Saving stock listings failed: %s', e)

    def refresh_stocks(self, stock_list: [] = []):
        """
        Refresh stock price history. It will first get latest symbols list from the web services provided in configs,
        then fetch stock price history from Yahoo! and Google, Yahoo! data take precedence over Google data.

        The data will store in the target location specified in configs. Each symbol stored in one file, naming as 
        %exchange%_%symbol%.csv

        Errors are logged in error.log file.

        :return: None 
        """
        total_symbols = 0
        symbols_no_data = 0
        yahoo_errors = 0
        google_errors = 0
        symbol_pattern = re.compile(r'^(\w|\.)+$')
        futures = []
        # purge stock history folder if refresh all (ie. stock_list is empty)
        if not stock_list:
            shutil.rmtree(Utility.get_data_folder(DataFolder.Stock_History, Market.US), ignore_errors=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.concurrent) as executor:
            with pandas.ExcelFile(Utility.get_stock_listing_xlsx(Market.US, latest=True)) as listings:
                for exchange in listings.sheet_names:
                    self.logger.info('Fetching stock history prices from exchange %s.', exchange.upper())
                    stocks = pandas.read_excel(listings, exchange)
                    for stock in stocks.itertuples():
                        if stock_list and stock.Symbol not in stock_list:
                            continue  # skip stock that is not in stock_list
                        if not symbol_pattern.match(stock.Symbol):
                            continue  # skip invalid symbols
                        if isinstance(stock.IPO, str):
                            start_date = Utility.get_config().history_start_date if stock.IPO == 'n/a' else stock.IPO
                        elif np.isnan(stock.IPO):
                            start_date = Utility.get_config().history_start_date
                        else:
                            start_date = str(int(stock.IPO))
                        start_date = pandas.to_datetime(start_date)
                        futures.append(executor.submit(self.refresh_stock, exchange, stock.Symbol, start_date))
                        total_symbols += 1

        for future in futures:
            (stock_prices, yahoo_error, google_error) = future.result()
            yahoo_errors += yahoo_error
            google_errors += google_error
            symbols_no_data += (2 == yahoo_error + google_error)
        self.logger.error(
            'Stock prices update completed, %s (%s) symbols has no data, yahoo has %s errors and google has %s errors.',
            symbols_no_data, total_symbols, yahoo_errors, google_errors)

    def refresh_stock(self, exchange: str, symbol: str, start_date: datetime, end_date=datetime.date.today()):
        history_prices = self._get_yahoo_data(exchange, symbol, start_date, end_date)
        if history_prices is None or history_prices.empty:
            history_prices = self._get_google_data(exchange, symbol, start_date, end_date)
        if history_prices is not None:
            history_prices.index.rename(StockPriceField.Date.value, inplace=True)
            history_prices.rename(columns={'Open': StockPriceField.Open.value,
                                           'High': StockPriceField.High.value,
                                           'Low': StockPriceField.Low.value,
                                           'Close': StockPriceField.Close.value,
                                           'Volume': StockPriceField.Volume.value}, inplace=True)
            history_prices.insert(0, StockPriceField.Symbol.value, symbol)
            history_prices.to_csv(Utility.get_stock_price_history_file(Market.US, symbol, start_date.year, exchange))
            self.logger.info('Updated price history for [%s] %s\t(%s - %s)', exchange.upper(), symbol,
                             start_date.date(), end_date)

        return history_prices, history_prices is None, history_prices is None

    def _get_yahoo_data(self, exchange, symbol, start, end):
        yahoo_data = None
        for i in range(self.retry):
            try:
                yahoo_data = web.get_data_yahoo(symbol.strip(), start, end + datetime.timedelta(days=1))
                yahoo_data['Close'] = yahoo_data['Adj Close']
                del yahoo_data['Adj Close']
                break
            except Exception as e:
                self.logger.error("Failed to get Yahoo! data for [%s] (%s) price history, %s",
                                  exchange.upper(), symbol, e)
        return yahoo_data

    def _get_google_data(self, exchange, symbol, start, end):
        google_data = None
        for i in range(self.retry):
            try:
                google_data = web.get_data_google(symbol.strip(), start, end)
                break
            except Exception as e:
                self.logger.error("Failed to get Google data for [%s] (%s) price history, %s",
                                  exchange.upper(), symbol, e)
        return google_data
