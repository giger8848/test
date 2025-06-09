#!/usr/bin/env python
# -*- coding: utf-8 -*-

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, scrolledtext
import queue
import json
import os
import logging
import threading
from datetime import datetime, timedelta
import ccxt
import pandas as pd
import time

# 로그 설정
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class MultiSymbolAutoTrader:
    """멀티 심볼 자동매매 엔진"""

    def __init__(self, settings, log_queue=None):
        self.settings = settings
        self.log_queue = log_queue
        self.exchange = self.initialize_exchange()
        self.symbol_states = {}
        
        self.calculate_levels()
        
        for symbol in self.settings['symbols']:
            if symbol.strip():
                self.symbol_states[symbol] = {
                    'balance': 0,
                    'current_position': None,
                    'current_orders': [],
                    'order_level': 0,
                    'last_close_time': None,
                    'is_first_entry': True,
                    'is_active': True,
                    'just_entered': False,
                    'tp_order_id': None,
                    'current_tp_price': None,
                    'current_tp_type': None,
                    'cumulative_amounts': {}
                }
        
        self.set_leverages()
        self.should_stop = False

    def calculate_levels(self):
        """자본 사용 비율에 따른 레벨 설정 계산"""
        base_levels = {
            '1': {'distance': 0, 'ratio': 5.0},
            '2': {'distance': 1.5, 'ratio': 5.0},
            '3': {'distance': 3.0, 'ratio': 7.5},
            '4': {'distance': 5.0, 'ratio': 10.0},
            '5': {'distance': 8.0, 'ratio': 12.5},
            '6': {'distance': 12.0, 'ratio': 15.0},
            '7': {'distance': 17.0, 'ratio': 20.0},
            '8': {'distance': 23.0, 'ratio': 25.0},
            '9': {'distance': 30.0, 'ratio': 30.0},
            '10': {'distance': 38.0, 'ratio': 40.0}
        }
        base_total_ratio = sum(level['ratio'] for level in base_levels.values())
        user_ratio = self.settings.get('capital_usage_ratio', 170)
        multiplier = user_ratio / base_total_ratio
        self.settings['levels'] = {}
        for level, config in base_levels.items():
            self.settings['levels'][level] = {
                'distance': config['distance'],
                'ratio': config['ratio'] * multiplier
            }

    def log(self, message):
        """로그 메시지 전송"""
        if self.log_queue:
            self.log_queue.put(message)
        print(message)

    def initialize_exchange(self):
        """거래소 API 초기화"""
        try:
            exchange_name = self.settings['selected_exchange'].lower()
            api_keys = self.settings['exchanges'][exchange_name]
            
            if exchange_name == 'okx':
                exchange = ccxt.okx({
                    'apiKey': api_keys['api_key'],
                    'secret': api_keys['secret_key'],
                    'password': api_keys['password'],
                    'options': {'defaultType': 'swap'},
                    'enableRateLimit': True
                })
                self.check_and_set_position_mode(exchange)
                self.log(f"{exchange_name.upper()} 거래소 API 연결 성공")
            
            elif exchange_name == 'binance':
                exchange = ccxt.binance({
                    'apiKey': api_keys['api_key'],
                    'secret': api_keys['secret_key'],
                    'enableRateLimit': True,
                    'options': {'defaultType': 'future'}
                })
                self.log(f"{exchange_name.upper()} 거래소 API 연결 성공")
            
            elif exchange_name == 'bybit':
                exchange = ccxt.bybit({
                    'apiKey': api_keys['api_key'],
                    'secret': api_keys['secret_key'],
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'swap',
                        'adjustForTimeDifference': True,
                        'recvWindow': 15000
                    }
                })
                self.check_and_set_position_mode(exchange)
                self.log(f"{exchange_name.upper()} 거래소 API 연결 성공")
            
            else:
                raise ValueError(f"지원하지 않는 거래소: {exchange_name}")
            
            exchange.load_time_difference()
            return exchange
        except Exception as e:
            error_msg = f"거래소 API 연결 오류: {str(e)}"
            self.log(error_msg)
            raise Exception(error_msg)

    def check_and_set_position_mode(self, exchange):
        """포지션 모드 확인 및 설정 (OKX, Bybit에 적용)"""
        try:
            if exchange.id in ['okx', 'bybit']:
                if exchange.id == 'okx':
                    try:
                        account_config = exchange.privateGetAccountConfig()
                        for config in account_config.get('data', []):
                            if 'instType' in config and config['instType'] == 'SWAP':
                                pos_mode = config.get('posMode', 'long_short_mode')
                                self.log(f"현재 포지션 모드: {pos_mode}")
                                if pos_mode != 'net_mode':
                                    result = exchange.privatePostAccountSetPositionMode({'posMode': 'net_mode'})
                                    if result.get('code') == '0':
                                        self.log("단방향 모드로 변경 성공")
                                        self.position_mode = 'net_mode'
                                    else:
                                        self.log(f"단방향 모드 변경 실패: {result}")
                                        raise Exception("단방향 모드 설정 실패")
                                else:
                                    self.position_mode = 'net_mode'
                                break
                        else:
                            self.log("SWAP instType 데이터 없음 - 단방향 모드 기본 적용")
                            self.position_mode = 'net_mode'
                    except Exception as e:
                        self.log(f"포지션 모드 확인/설정 실패: {str(e)}")
                        self.position_mode = 'net_mode'  # 기본값 적용
                elif exchange.id == 'bybit':
                    try:
                        mode_response = exchange.privateGetPositionMode()
                        mode = mode_response.get('result', {}).get('mode', 1)
                        self.log(f"현재 포지션 모드: {mode}")
                        if mode != 0:
                            exchange.privatePostPositionSwitchMode({'mode': 0})
                            self.log("단방향 모드로 변경 성공")
                            self.position_mode = 'net_mode'
                        else:
                            self.position_mode = 'net_mode'
                    except (AttributeError, KeyError, Exception) as e:
                        self.log(f"포지션 모드 API 호출 실패 - 기본값(net_mode) 사용: {str(e)}")
                        self.position_mode = 'net_mode'
        except Exception as e:
            self.log(f"포지션 모드 확인/설정 실패: {str(e)}")
            self.position_mode = 'net_mode'

    def set_leverages(self):
        """레버리지 설정"""
        for symbol in self.settings['symbols']:
            if symbol.strip():
                try:
                    leverage = self.settings['leverage']
                    current_leverage = self.exchange.fetch_leverage(symbol) or 1
                    if current_leverage != leverage:
                        self.exchange.set_leverage(leverage, symbol)
                        self.log(f"{symbol} 레버리지 설정 완료: {leverage}x")
                    else:
                        self.log(f"{symbol} 레버리지 이미 {leverage}x로 설정됨")
                except Exception as e:
                    self.log(f"{symbol} 레버리지 설정 실패: {str(e)}")

    def fetch_balance(self):
        """잔액 조회"""
        try:
            exchange_name = self.settings['selected_exchange'].lower()
            if exchange_name == 'okx':
                balance = self.exchange.fetch_balance(params={'type': 'swap'})['total'].get('USDT', 0)
            elif exchange_name == 'binance':
                balance = self.exchange.fetch_balance(params={'type': 'future'})['total'].get('USDT', 0)
            elif exchange_name == 'bybit':
                balance = self.exchange.fetch_balance(params={'type': 'swap'})['total'].get('USDT', 0)
            return balance
        except Exception as e:
            self.log(f"잔액 조회 오류: {str(e)}")
            return 0

    def fetch_ticker(self, symbol):
        """시세 조회"""
        try:
            ticker = self.exchange.fetch_ticker(symbol)
            return ticker['last']
        except Exception as e:
            self.log(f"{symbol} 시세 조회 오류: {str(e)}")
            raise Exception(f"{symbol} 시세 조회 오류: {str(e)}")

    def fetch_ohlcv(self, symbol, timeframe='1h', limit=80):
        """OHLCV 데이터 조회"""
        try:
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            return ohlcv
        except Exception as e:
            self.log(f"{symbol} OHLCV 데이터 조회 오류: {str(e)}")
            return []

    def get_price_precision(self, price, symbol=None):
        """가격과 심볼에 따른 적절한 소수점 자릿수 반환"""
        if symbol:
            try:
                market = self.exchange.market(symbol)
                precision = market.get('precision', {}).get('price', 8)
                return precision
            except Exception:
                pass
        if price >= 1000:
            return 2
        elif price >= 100:
            return 3
        elif price >= 10:
            return 4
        elif price >= 1:
            return 5
        elif price >= 0.1:
            return 6
        elif price >= 0.01:
            return 7
        else:
            return 8

    def format_price(self, price, symbol=None):
        """가격을 심볼에 맞는 소수점으로 포맷팅"""
        precision = self.get_price_precision(price, symbol)
        return f"${price:.{precision}f}"

    def fetch_current_position(self, symbol):
        """포지션 조회"""
        try:
            for attempt in range(5):
                try:
                    positions = self.exchange.fetch_positions([symbol])
                    for position in positions:
                        if position['symbol'] == symbol:
                            contracts = float(position.get('contracts', 0)) if position.get('contracts') is not None else 0
                            entry_price = float(position.get('entryPrice', 0)) if position.get('entryPrice') is not None else 0
                            if contracts > 0:
                                return {
                                    'side': position.get('side', 'long'),
                                    'amount': contracts,
                                    'entry_price': entry_price,
                                    'leverage': float(position.get('leverage', 1)),
                                    'unrealized_pnl': float(position.get('unrealizedPnl', 0))
                                }
                    return None
                except Exception as e:
                    self.log(f"{symbol} 포지션 조회 시도 {attempt + 1}/5 실패: {str(e)}")
                    if attempt < 4:
                        time.sleep(2)
                    else:
                        self.log(f"{symbol} 포지션 조회 오류: {str(e)}")
                        return None
        except Exception as e:
            self.log(f"{symbol} 포지션 조회 오류: {str(e)}")
            return None

    def fetch_open_orders(self, symbol):
        """미체결 주문 조회"""
        try:
            return self.exchange.fetch_open_orders(symbol)
        except Exception as e:
            self.log(f"{symbol} 주문 조회 오류: {str(e)}")
            return []

    def cancel_all_orders(self, symbol):
        """모든 미체결 주문 취소"""
        try:
            open_orders = self.fetch_open_orders(symbol)
            if not open_orders:
                return
            for order in open_orders:
                try:
                    self.exchange.cancel_order(order['id'], symbol)
                    self.log(f"{symbol} 주문 취소: ID {order['id']}")
                except Exception as e:
                    self.log(f"{symbol} 주문 취소 실패: ID {order['id']}, 오류: {str(e)}")
            self.log(f"{symbol} 총 {len(open_orders)}개 주문 취소 완료")
        except Exception as e:
            self.log(f"{symbol} 주문 취소 오류: {str(e)}")

    def cancel_tp_order(self, symbol):
        """TP 주문만 취소"""
        try:
            state = self.symbol_states[symbol]
            if state['tp_order_id']:
                try:
                    self.exchange.cancel_order(state['tp_order_id'], symbol)
                    self.log(f"{symbol} TP 주문 취소: ID {state['tp_order_id']}")
                    state['tp_order_id'] = None
                    state['current_tp_price'] = None
                    state['current_tp_type'] = None
                except Exception as e:
                    self.log(f"{symbol} TP 주문 취소 실패: {str(e)}")
        except Exception as e:
            self.log(f"{symbol} TP 주문 취소 오류: {str(e)}")

    def calculate_order_amount(self, level, price, total_balance, symbol):
        """주문 수량 계산"""
        try:
            level_str = str(level)
            base_ratio = self.settings['levels'][level_str]['ratio'] / 100.0
            active_symbols_count = len([s for s in self.settings['symbols'] if s.strip()])
            symbol_ratio = base_ratio / active_symbols_count
            capital_multiplier = self.settings.get('capital_usage_ratio', 170) / 170.0
            final_ratio = symbol_ratio * capital_multiplier
            position_value = total_balance * final_ratio
            
            market = self.exchange.market(symbol)
            contract_size = market.get('contractSize', 1)
            contracts = position_value / (price * contract_size)
            
            if 'precision' in market and 'amount' in market['precision']:
                precision = market['precision']['amount']
                if isinstance(precision, int):
                    contracts = round(contracts, precision)
                else:
                    contracts = float(round(contracts, 8))
            else:
                contracts = float(round(contracts, 8))
            
            min_amount = 0.001
            if 'limits' in market and 'amount' in market['limits']:
                market_min = market['limits']['amount'].get('min', min_amount)
                min_amount = max(min_amount, market_min)
            
            if contracts < min_amount:
                contracts = min_amount
            
            return contracts
        except Exception as e:
            self.log(f"{symbol} 주문 수량 계산 오류: {str(e)}")
            contracts = position_value / price
            return max(float(round(contracts, 8)), 0.001)


    def place_market_order(self, symbol, side, amount):
        """시장가 주문"""
        try:
            params = {}
            exchange_name = self.settings['selected_exchange'].lower()
            if exchange_name == 'binance':
                # markets 데이터 로드
                markets = self.exchange.load_markets()
                if symbol in markets:
                    market = markets[symbol]
                    precision = market['precision']['amount']  # 소수점 정밀도
                    lot_size = float(market['limits']['amount']['min'])  # 최소 주문 수량
                else:
                    precision = 8  # 기본 정밀도
                    lot_size = 0.01  # 기본 최소 주문 수량
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                min_notional = 5.0
                required_amount = max(amount, (min_notional / current_price) * 1.1)
                # 정밀도에 맞게 반올림
                rounded_amount = round(required_amount, precision)
                # lot_size 배수로 조정, 정수화
                multiplier = int(round(rounded_amount / lot_size))
                adjusted_amount = int(multiplier * lot_size)  # 명시적 정수 변환
                if adjusted_amount < int(lot_size):
                    adjusted_amount = int(lot_size)
                self.log(f"Debug - Symbol: {symbol}, Required: {required_amount}, Rounded: {rounded_amount}, Adjusted: {adjusted_amount}, Lot Size: {lot_size}")
            elif exchange_name == 'bybit':
                if side == 'buy':
                    params['posSide'] = 'long'
                elif side == 'sell':
                    params['posSide'] = 'short'
            elif exchange_name == 'okx':
                params['tdMode'] = 'cross'
                if hasattr(self, 'position_mode') and self.position_mode == 'long_short_mode':
                    params['posSide'] = 'long' if side == 'buy' else 'short'
            
            order = self.exchange.create_market_order(symbol, side, adjusted_amount, params=params)
            self.log(f"{symbol} {side} 시장가 주문 성공: 계약수 {adjusted_amount}")
            self.generate_followup_orders(symbol, side, adjusted_amount)  # 후속 주문 트리거
            return order
        except Exception as e:
            self.log(f"{symbol} 시장가 주문 오류: {str(e)}")
            return None

    def place_limit_order(self, symbol, side, price, amount, level):
        """지정가 주문"""
        try:
            params = {}
            exchange_name = self.settings['selected_exchange'].lower()
            if exchange_name == 'binance':
                params['reduceOnly'] = False
            elif exchange_name == 'bybit':
                if side == 'buy':
                    params['posSide'] = 'long'
                elif side == 'sell':
                    params['posSide'] = 'short'
            elif exchange_name == 'okx':
                params['tdMode'] = 'cross'
                if hasattr(self, 'position_mode') and self.position_mode == 'long_short_mode':
                    params['posSide'] = 'long' if side == 'buy' else 'short'
            
            order = self.exchange.create_limit_order(symbol, side, amount, price, params=params)
            self.log(f"{symbol} {level}회차 {side} 지정가 주문 실행: 가격 {self.format_price(price)}, 수량 {amount}")
            return order
        except Exception as e:
            self.log(f"{symbol} 지정가 주문 오류: {str(e)}")
            return None

    def generate_followup_orders(self, symbol, side, initial_amount):
        """시장가 주문 후 후속 지정가 주문 생성 (기존 로직 기반)"""
        try:
            exchange_name = self.settings['selected_exchange'].lower()
            if exchange_name == 'binance':
                position = self.exchange.fetch_position(symbol)
                if position and float(position['info']['positionAmt']) != 0:
                    base_price = float(self.exchange.fetch_ticker(symbol)['last'])
                    for i in range(1, 6):  # 5회차 후속 주문
                        price_step = base_price * (1 + 0.01 * i)  # 1%씩 증가
                        amount_step = initial_amount * 0.2  # 20%씩 분할
                        self.place_limit_order(symbol, "sell" if side == "buy" else "buy", price_step, amount_step, i)
                else:
                    self.log(f"{symbol} 포지션 없음, 후속 주문 생성 실패")
            elif exchange_name in ['bybit', 'okx']:
                # 기존 OKX 로직 유지 (포지션 기반 후속 주문)
                state = self.symbol_states.get(symbol, {})
                if state.get('position_amount', 0) > 0:
                    base_price = float(self.exchange.fetch_ticker(symbol)['last'])
                    for i in range(1, 6):
                        price_step = base_price * (1 + 0.01 * i)
                        amount_step = initial_amount * 0.2
                        self.place_limit_order(symbol, "sell" if side == "buy" else "buy", price_step, amount_step, i)
        except Exception as e:
            self.log(f"{symbol} 후속 주문 생성 오류: {str(e)}")

    def place_tp_order(self, symbol, tp_price, amount, tp_type):
        """TP 주문 생성 - 에러 처리 강화"""
        try:
            params = {'reduceOnly': True}
            exchange_name = self.settings['selected_exchange'].lower()
            if exchange_name == 'okx':
                params['tdMode'] = 'cross'  # OKX 크로스 마진 모드 설정
                # 단방향 모드에서는 posSide 생략, 양방향 모드에서만 설정
                if hasattr(self, 'position_mode') and self.position_mode == 'long_short_mode':
                    params['posSide'] = 'long'  # TP는 long 포지션 청산
            elif exchange_name == 'bybit':
                params['posSide'] = 'long'  # TP는 long 포지션 청산
            elif exchange_name == 'binance':
                params['reduceOnly'] = True
            
            order = self.exchange.create_limit_order(symbol, "sell", amount, tp_price, params=params)
            
            if order and order.get('id'):
                state = self.symbol_states[symbol]
                state['tp_order_id'] = order['id']
                state['current_tp_price'] = tp_price
                state['current_tp_type'] = tp_type
                
                tp_type_korean = "익절" if tp_type == "profit" else "손절"
                self.log(f"{symbol} {tp_type_korean} TP 주문 생성: 가격 {self.format_price(tp_price)}, 수량 {amount}, ID {order['id']}")
                return order
            else:
                self.log(f"{symbol} TP 주문 생성 실패 - 주문 정보 없음")
                return None
        except Exception as e:
            self.log(f"{symbol} TP 주문 생성 오류: {str(e)}")
            return None

    def check_tp_execution(self, symbol, position):
        """TP 주문 체결 확인"""
        try:
            state = self.symbol_states[symbol]
            if not state.get('tp_order_id'):
                return False
            try:
                tp_order = self.exchange.fetch_open_order(state['tp_order_id'], symbol, params={"acknowledged": True})
                if tp_order and tp_order['status'] in ['closed', 'filled']:
                    tp_type_korean = "익절" if state.get('current_tp_type') == "profit" else "손절"
                    self.log(f"{symbol} {tp_type_korean} TP 주문 체결 감지 - 포지션 정리 시작")
                    self.reset_symbol_state_after_close(symbol)
                    return True
            except Exception as order_error:
                self.log(f"{symbol} TP 주문 조회 실패: {str(order_error)}")
                if not position:
                    self.log(f"{symbol} 포지션 없음 + TP 주문 조회 실패 = TP 체결로 판단")
                    self.reset_symbol_state_after_close(symbol)
                    return True
            return False
        except Exception as e:
            self.log(f"{symbol} TP 체결 확인 오류: {str(e)}")
            return False

    def reset_symbol_state_after_close(self, symbol):
        """포지션 청산 후 상태 초기화"""
        try:
            state = self.symbol_states[symbol]
            self.log(f"{symbol} 청산 완료 - 모든 미체결 주문 취소 시작")
            self.cancel_all_orders(symbol)
            time.sleep(1)
            state['last_close_time'] = datetime.now()
            state['is_first_entry'] = False
            state['order_level'] = 0
            state['just_entered'] = False
            state['tp_order_id'] = None
            state['current_tp_price'] = None
            state['current_tp_type'] = None
            state['cumulative_amounts'] = {}
            self.log(f"{symbol} 상태 초기화 완료 - 60초 후 재진입 가능")
        except Exception as e:
            self.log(f"{symbol} 상태 초기화 오류: {str(e)}")

    def check_position_status_change(self, symbol, prev_position, current_position):
        """포지션 상태 변화 감지 및 처리"""
        try:
            state = self.symbol_states[symbol]
            if prev_position and not current_position:
                if state.get('tp_order_id'):
                    tp_type_korean = "익절" if state.get('current_tp_type') == "profit" else "손절"
                    self.log(f"{symbol} 포지션 청산 감지 - {tp_type_korean} 완료")
                else:
                    self.log(f"{symbol} 포지션 청산 감지 - 수동 청산으로 추정")
                self.log(f"{symbol} 청산 감지 즉시 - 모든 주문 취소 실행")
                self.cancel_all_orders(symbol)
                time.sleep(0.5)
                self.reset_symbol_state_after_close(symbol)
                return True
            return False
        except Exception as e:
            self.log(f"{symbol} 포지션 상태 변화 확인 오류: {str(e)}")
            return False

    def update_tp_order(self, symbol, position):
        """TP 주문 업데이트"""
        try:
            if not position or position['side'] != 'long':
                return
            state = self.symbol_states[symbol]
            take_profit_percent = self.settings.get('take_profit_percent', 1.0) / 100.0
            target_tp_price = position['entry_price'] * (1 + take_profit_percent)
            target_tp_type = "profit"
            current_tp = state.get('current_tp_price')
            current_type = state.get('current_tp_type')
            price_changed = not current_tp or abs(current_tp - target_tp_price) > 0.01
            type_changed = current_type != target_tp_type
            if price_changed or type_changed:
                if state['tp_order_id']:
                    self.cancel_tp_order(symbol)
                    time.sleep(0.5)
                tp_order = self.place_tp_order(symbol, target_tp_price, position['amount'], target_tp_type)
                if tp_order:
                    self.log(f"{symbol} TP 업데이트 완료: 익절 {self.format_price(target_tp_price)}")
                else:
                    self.log(f"{symbol} TP 주문 생성 실패")
        except Exception as e:
            self.log(f"{symbol} TP 업데이트 오류: {str(e)}")

    def close_position_market(self, symbol):
        """포지션 청산 (긴급시 사용)"""
        position = self.fetch_current_position(symbol)
        if position and position['side'] == 'long':
            try:
                params = {'posSide': 'long', "reduceOnly": True}
                exchange_name = self.settings['selected_exchange'].lower()
                if exchange_name == 'okx':
                    params.update({'tdMode': 'cross'})  # 크로스 마진 모드 설정
                if exchange_name in ['okx', 'bybit']:
                    params['posSide'] = 'long'
                close_order = self.exchange.create_market_order(symbol, "sell", position["amount"], params=params)
                self.log(f"{symbol} 롱 포지션 시장가 청산: {position['amount']}")
                self.symbol_states[symbol]['last_close_time'] = datetime.now()
                self.symbol_states[symbol]['is_first_entry'] = False
                self.symbol_states[symbol]['cumulative_amounts'] = {}
                return close_order
            except Exception as e:
                self.log(f"{symbol} 포지션 청산 오류: {str(e)}")
                return None
        return None

    def close_all_positions(self):
        """모든 포지션 청산 (긴급 정지용)"""
        self.log("모든 포지션 청산 시작")
        for symbol in self.settings['symbols']:
            if symbol.strip():
                try:
                    position = self.fetch_current_position(symbol)
                    if position:
                        self.close_position_market(symbol)
                        time.sleep(1)
                except Exception as e:
                    self.log(f"{symbol} 포지션 청산 오류: {str(e)}")

    def place_initial_long_order(self, symbol, current_price, total_balance):
        """초기 롱 주문 생성"""
        level = 1
        long_amount = self.calculate_order_amount(level, current_price, total_balance, symbol)
        long_order = self.place_market_order(symbol, "buy", long_amount)
        if long_order:
            self.log(f"{symbol} 1회차 롱 시장가 주문 생성 완료 - 수량: {long_amount}")
            return True
        return False

    def place_all_next_level_orders(self, symbol, entry_price, total_balance):
        """후속 회차 주문 생성"""
        self.log(f"{symbol} 롱 포지션 진입 후 모든 후속 회차 주문 생성 시작")
        max_level = 10 if '10' in self.settings['levels'] else 9
        for level in range(2, max_level + 1):
            level_str = str(level)
            if level_str in self.settings['levels']:
                distance = self.settings['levels'][level_str]['distance'] / 100.0
                next_price = entry_price * (1 - distance)
                amount = self.calculate_order_amount(level, next_price, total_balance, symbol)
                order = self.place_limit_order(symbol, "buy", next_price, amount, level)
                if order:
                    self.log(f"{symbol} {level}회차 롱 주문 생성 완료 - 가격: {self.format_price(next_price)}, 수량: {amount}")
        self.symbol_states[symbol]['order_level'] = max_level
        self.log(f"{symbol} 모든 후속 회차 주문 생성 완료")

    def calculate_cumulative_amounts(self, symbol, entry_price, total_balance):
        """각 차수별 누적 수량 테이블 생성"""
        try:
            state = self.symbol_states[symbol]
            cumulative_amounts = {}
            cumulative_total = 0
            for level in range(1, 11):
                level_str = str(level)
                if level_str in self.settings['levels']:
                    amount = self.calculate_order_amount(level, entry_price, total_balance, symbol)
                    cumulative_total += amount
                    cumulative_amounts[level] = cumulative_total
            state['cumulative_amounts'] = cumulative_amounts
            self.log(f"{symbol} 누적 수량 테이블 생성 완료")
        except Exception as e:
            self.log(f"{symbol} 누적 수량 테이블 생성 오류: {str(e)}")

    def get_current_level_from_position(self, symbol, position_amount):
        """포지션 수량을 기준으로 현재 진입 차수 계산"""
        try:
            state = self.symbol_states[symbol]
            cumulative_amounts = state.get('cumulative_amounts', {})
            if not cumulative_amounts:
                return 1
            closest_level = 1
            min_diff = float('inf')
            for level, cumulative_amount in cumulative_amounts.items():
                diff = abs(position_amount - cumulative_amount)
                if diff < min_diff:
                    min_diff = diff
                    closest_level = level
            return closest_level
        except Exception as e:
            self.log(f"{symbol} 현재 차수 계산 오류: {str(e)}")
            return 1

    def can_enter_position(self, symbol):
        """포지션 진입 가능 여부 확인"""
        state = self.symbol_states[symbol]
        if state['is_first_entry']:
            return True
        if state['last_close_time']:
            elapsed = datetime.now() - state['last_close_time']
            wait_time = timedelta(seconds=60)
            return elapsed >= wait_time
        return True

    def process_symbol(self, symbol, total_balance):
        """개별 종목 처리"""
        try:
            if not self.symbol_states[symbol]['is_active']:
                return
            current_price = self.fetch_ticker(symbol)
            position = self.fetch_current_position(symbol)
            open_orders = self.fetch_open_orders(symbol)
            state = self.symbol_states[symbol]
            prev_position = state['current_position']
            if not position and open_orders:
                self.log(f"{symbol} 포지션 없음 + 미체결 주문 {len(open_orders)}개 감지 - 모든 주문 취소")
                self.cancel_all_orders(symbol)
                time.sleep(1)
                open_orders = []
            if self.check_position_status_change(symbol, prev_position, position):
                state['current_position'] = position
                state['current_orders'] = []
                return
            if position and position['side'] == 'long':
                if self.check_tp_execution(symbol, position):
                    state['current_position'] = None
                    state['current_orders'] = []
                    return
            if not position:
                state['just_entered'] = False
                if not open_orders and self.can_enter_position(symbol):
                    self.log(f"{symbol} 진입 조건 충족 - 롱 진입 시도")
                    if self.place_initial_long_order(symbol, current_price, total_balance):
                        state['order_level'] = 1
                        state['just_entered'] = True
                        time.sleep(3)
                        for _ in range(3):
                            new_position = self.fetch_current_position(symbol)
                            if new_position and new_position['side'] == 'long':
                                self.log(f"{symbol} 시장가 진입 확인됨, 후속 주문 생성")
                                self.place_all_next_level_orders(symbol, new_position['entry_price'], total_balance)
                                self.calculate_cumulative_amounts(symbol, new_position['entry_price'], total_balance)
                                self.update_tp_order(symbol, new_position)
                                break
                            time.sleep(1)
                elif open_orders:
                    if self.can_enter_position(symbol):
                        self.log(f"{symbol} 진입 대기시간 완료 - 기존 주문 유지")
                    else:
                        remaining_time = 60 - (datetime.now() - state['last_close_time']).total_seconds()
                        self.log(f"{symbol} 재진입 대기 중 - {remaining_time:.0f}초 남음")
            elif position and position['side'] == 'long':
                if not state.get('just_entered', False):
                    self.log(f"{symbol} 새 롱 포지션 감지: {position['amount']} @ {position['entry_price']}")
                    if open_orders:
                        self.cancel_all_orders(symbol)
                        time.sleep(1)
                    self.place_all_next_level_orders(symbol, position['entry_price'], total_balance)
                    state['just_entered'] = True
                    self.calculate_cumulative_amounts(symbol, position['entry_price'], total_balance)
                    self.update_tp_order(symbol, position)
                self.update_tp_order(symbol, position)
            state['current_position'] = position
            state['current_orders'] = open_orders
        except Exception as e:
            self.log(f"{symbol} 처리 오류: {str(e)}")

    def show_status(self):
        """현재 상태 출력"""
        status_msg = "\n===== 현재 상태 ====="
        self.log(status_msg)
        for symbol in self.settings['symbols']:
            if not symbol.strip():
                continue
            try:
                current_price = self.fetch_ticker(symbol)
                position = self.fetch_current_position(symbol)
                open_orders = self.fetch_open_orders(symbol)
                state = self.symbol_states[symbol]
                self.log(f"\n--- {symbol} ---")
                self.log(f"현재가: {self.format_price(current_price)}")
                self.log(f"활성화: {'예' if state['is_active'] else '아니오'}")
                if position:
                    self.log(f"포지션: {position['side']} {position['amount']:.6f}")
                    self.log(f"진입가: {self.format_price(position['entry_price'])}")
                    self.log(f"미실현 손익: ${position['unrealized_pnl']:.2f}")
                    if state['current_tp_price']:
                        self.log(f"활성 TP: 익절 {self.format_price(state['current_tp_price'])}")
                else:
                    self.log("포지션: 없음")
                self.log(f"미체결 주문: {len(open_orders)}개")
                if state['last_close_time']:
                    elapsed = datetime.now() - state['last_close_time']
                    wait_time = timedelta(seconds=60)
                    remaining = max(0, (wait_time - elapsed).total_seconds())
                    self.log(f"진입 대기시간: {remaining:.0f}초 남음")
            except Exception as e:
                self.log(f"{symbol} 상태 조회 오류: {str(e)}")
        try:
            total_balance = self.fetch_balance()
            self.log(f"\n총 잔액: ${total_balance:.2f} USDT")
        except Exception as e:
            self.log(f"잔액 조회 오류: {str(e)}")
        self.log("=" * 50)

    def run(self):
        """메인 실행 루프"""
        self.log("자동매매 시작")
        while not self.should_stop:
            try:
                total_balance = self.fetch_balance()
                for symbol in self.settings['symbols']:
                    if self.should_stop:
                        break
                    if symbol.strip():
                        self.process_symbol(symbol, total_balance)
                        time.sleep(1)
                if hasattr(self, 'last_status_time'):
                    if datetime.now() - self.last_status_time > timedelta(minutes=10):
                        self.show_status()
                        self.last_status_time = datetime.now()
                else:
                    self.show_status()
                    self.last_status_time = datetime.now()
                time.sleep(30)
            except Exception as e:
                self.log(f"메인 루프 오류: {str(e)}")
                time.sleep(10)
        self.log("자동매매 종료")

    def stop(self):
        """자동매매 중지"""
        self.should_stop = True
        self.log("자동매매 중지 요청됨")

    def emergency_stop(self):
        """긴급 정지 - 모든 주문 취소 및 포지션 청산"""
        self.log("긴급 정지 시작 - 모든 주문 취소 및 포지션 청산")
        for symbol in self.settings['symbols']:
            if symbol.strip():
                try:
                    self.cancel_all_orders(symbol)
                    time.sleep(0.5)
                except Exception as e:
                    self.log(f"{symbol} 긴급 주문 취소 오류: {str(e)}")
        self.close_all_positions()
        self.stop()


class TradingGUI:
    """기본형 GUI 인터페이스"""
    
    def __init__(self):
        self.trader = None
        self.trading_thread = None
        self.log_queue = queue.Queue()
        self.api_keys = {  # GUI에서 관리할 API 키 캐시
            'okx': {'api_key': '', 'secret_key': '', 'password': ''},
            'binance': {'api_key': '', 'secret_key': '', 'password': ''},
            'bybit': {'api_key': '', 'secret_key': '', 'password': ''}
        }
        
        self.root = tk.Tk()
        self.root.title("Crypto Futures Trading Bot")
        self.root.geometry("1000x700")
        
        self.setup_gui()
        self.load_settings()
        
        self.root.after(100, self.update_logs)

    def setup_gui(self):
        """GUI 레이아웃 설정"""
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill='both', expand=True, padx=10, pady=10)
        
        title_label = ttk.Label(main_frame, text="Futures Trading Bot", 
                               font=('Arial', 16, 'bold'))
        title_label.pack(pady=(0, 20))
        
        settings_frame = ttk.LabelFrame(main_frame, text="거래 설정")
        settings_frame.pack(fill='both', expand=True, pady=(0, 10))
        
        self.notebook = ttk.Notebook(settings_frame)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=10)
        
        self.setup_api_tab()
        self.setup_trading_tab()
        
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill='x', pady=(0, 10))
        
        button_frame = ttk.Frame(control_frame)
        button_frame.pack(side='left')
        
        self.start_btn = ttk.Button(button_frame, text="매매 시작", 
                                   command=self.start_trading, state='normal')
        self.start_btn.pack(side='left', padx=5)
        
        self.stop_btn = ttk.Button(button_frame, text="매매 중지", 
                                  command=self.stop_trading, state='normal')
        self.stop_btn.pack(side='left', padx=5)
        
        self.emergency_btn = ttk.Button(button_frame, text="긴급 정지", 
                                       command=self.emergency_stop, state='normal')
        self.emergency_btn.pack(side='left', padx=5)
        
        self.status_label = ttk.Label(control_frame, text="상태: 대기 중")
        self.status_label.pack(side='right')
        
        log_frame = ttk.LabelFrame(main_frame, text="실시간 로그")
        log_frame.pack(fill='both', expand=True)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, 
                                                 font=('Consolas', 9))
        self.log_text.pack(fill='both', expand=True, padx=10, pady=10)
        
        self.setup_menu()

    def setup_api_tab(self):
        """API 설정 탭"""
        api_frame = ttk.Frame(self.notebook)
        self.notebook.add(api_frame, text="API 설정")
        
        ttk.Label(api_frame, text="거래소 선택:").grid(row=0, column=0, sticky='w', padx=5, pady=5)
        self.exchange_var = tk.StringVar(value='Bybit')
        exchange_dropdown = ttk.Combobox(
            api_frame,
            textvariable=self.exchange_var,
            values=['OKX', 'Binance', 'Bybit'],
            state='readonly',
            width=47
        )
        exchange_dropdown.grid(row=0, column=1, padx=5, pady=5, sticky='ew')
        exchange_dropdown.bind('<<ComboboxSelected>>', self.on_exchange_select)

        ttk.Label(api_frame, text="API Key:").grid(row=1, column=0, sticky='w', padx=5, pady=5)
        self.api_key_entry = ttk.Entry(api_frame, width=50, show='*')
        self.api_key_entry.grid(row=1, column=1, padx=5, pady=5, sticky='ew')
        
        ttk.Label(api_frame, text="Secret Key:").grid(row=2, column=0, sticky='w', padx=5, pady=5)
        self.secret_key_entry = ttk.Entry(api_frame, width=50, show='*')
        self.secret_key_entry.grid(row=2, column=1, padx=5, pady=5, sticky='ew')
        
        ttk.Label(api_frame, text="Password:").grid(row=3, column=0, sticky='w', padx=5, pady=5)
        self.password_entry = ttk.Entry(api_frame, width=50, show='*')
        self.password_entry.grid(row=3, column=1, padx=5, pady=5, sticky='ew')

    def on_exchange_select(self, event):
        """드롭다운 선택 시 호출되는 콜백"""
        selected_exchange = self.exchange_var.get().lower()
        self.log_text.insert(tk.END, f"[INFO] 거래소 선택됨: {selected_exchange}\n")
        self.log_text.see(tk.END)
        # 저장된 API 키로 GUI 업데이트
        api_keys = self.api_keys.get(selected_exchange, {'api_key': '', 'secret_key': '', 'password': ''})
        self.api_key_entry.delete(0, tk.END)
        self.api_key_entry.insert(0, api_keys['api_key'])
        self.secret_key_entry.delete(0, tk.END)
        self.secret_key_entry.insert(0, api_keys['secret_key'])
        self.password_entry.delete(0, tk.END)
        self.password_entry.insert(0, api_keys['password'])
        # symbols 입력 필드 업데이트
        entries = [self.symbol1_entry, self.symbol2_entry, self.symbol3_entry]
        for entry in entries:
            symbol = entry.get().strip()
            if symbol:
                if selected_exchange == 'binance':
                    symbol = symbol.replace(':USDT', '')
                elif selected_exchange in ['okx', 'bybit'] and not symbol.endswith(':USDT'):
                    symbol = f"{symbol}:USDT"
                entry.delete(0, tk.END)
                entry.insert(0, symbol)

    def setup_trading_tab(self):
        """거래 설정 탭"""
        trading_frame = ttk.Frame(self.notebook)
        self.notebook.add(trading_frame, text="거래 설정")
        
        ttk.Label(trading_frame, text="거래 종목:").grid(row=0, column=0, sticky='w', padx=5, pady=5)
        
        symbol_frame = ttk.Frame(trading_frame)
        symbol_frame.grid(row=0, column=1, sticky='w', padx=5, pady=5)
        
        self.symbol1_entry = ttk.Entry(symbol_frame, width=20)
        self.symbol1_entry.pack(side='left', padx=(0, 5))
        self.symbol1_entry.insert(0, "XRP/USDT:USDT")
        
        self.symbol2_entry = ttk.Entry(symbol_frame, width=20)
        self.symbol2_entry.pack(side='left', padx=5)
        self.symbol2_entry.insert(0, "DOGE/USDT:USDT")
        
        self.symbol3_entry = ttk.Entry(symbol_frame, width=20)
        self.symbol3_entry.pack(side='left', padx=5)
        self.symbol3_entry.insert(0, "ADA/USDT:USDT")
        
        ttk.Label(trading_frame, text="레버리지:").grid(row=1, column=0, sticky='w', padx=5, pady=5)
        self.leverage_entry = ttk.Entry(trading_frame, width=20)
        self.leverage_entry.grid(row=1, column=1, sticky='w', padx=5, pady=5)
        self.leverage_entry.insert(0, "3")
        
        ttk.Label(trading_frame, text="익절 퍼센트 (%):").grid(row=2, column=0, sticky='w', padx=5, pady=5)
        self.take_profit_entry = ttk.Entry(trading_frame, width=20)
        self.take_profit_entry.grid(row=2, column=1, sticky='w', padx=5, pady=5)
        self.take_profit_entry.insert(0, "1.0")
        
        ttk.Label(trading_frame, text="자본 사용 비율 (%):").grid(row=3, column=0, sticky='w', padx=5, pady=5)
        self.capital_usage_entry = ttk.Entry(trading_frame, width=20)
        self.capital_usage_entry.grid(row=3, column=1, sticky='w', padx=5, pady=5)
        self.capital_usage_entry.insert(0, "50")  # 170%에서 50%로 안전하게 조정
        
        ttk.Label(trading_frame, text="손절허용 레벨:").grid(row=4, column=0, sticky='w', padx=5, pady=5)
        self.donchian_level_entry = ttk.Entry(trading_frame, width=20)
        self.donchian_level_entry.grid(row=4, column=1, sticky='w', padx=5, pady=5)
        self.donchian_level_entry.insert(0, "6")
        
        ttk.Label(trading_frame, text="헷지:").grid(row=5, column=0, sticky='w', padx=5, pady=5)
        self.hedge_var = tk.BooleanVar()
        self.hedge_checkbox = ttk.Checkbutton(trading_frame, variable=self.hedge_var)
        self.hedge_checkbox.grid(row=5, column=1, sticky='w', padx=5, pady=5)

    def setup_menu(self):
        """메뉴 설정"""
        menubar = tk.Menu(self.root)
        self.root.config(menu=menubar)
        
        file_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="파일", menu=file_menu)
        file_menu.add_command(label="설정 저장", command=self.save_settings)
        file_menu.add_command(label="설정 불러오기", command=self.load_settings_file)
        file_menu.add_separator()
        file_menu.add_command(label="종료", command=self.root.quit)
        
        help_menu = tk.Menu(menubar, tearoff=0)
        menubar.add_cascade(label="사용법", menu=help_menu)
        help_menu.add_command(label="최초 세팅 방법", command=self.show_initial_setup)
        help_menu.add_command(label="사용 설명서", command=self.show_user_manual)

    def show_initial_setup(self):
        """최초 세팅 방법 표시"""
        setup_text = """
=== 최초 세팅 방법 ===
1. 거래소 API 키 설정:
   - OKX, Binance, Bybit의 API 키, 시크릿 키, 패스워드(OKX 전용)를 준비하세요.
   - 'API 설정' 탭에서 거래소를 선택하고 키를 입력하거나, JSON 설정 파일을 불러오세요.
2. JSON 설정 파일:
   - '파일 > 설정 저장'으로 API 키와 거래 설정을 settings.json에 저장.
   - 예시: {"exchanges": {"okx": {"api_key": "...", "secret_key": "...", "password": "..."}, ...}, "selected_exchange": "OKX", ...}
   - '파일 > 설정 불러오기'로 저장된 설정을 불러옵니다.
3. 거래 설정:
   - 거래 종목(예: XRP/USDT:USDT), 레버리지, 익절 퍼센트, 자본 사용 비율, 손절허용 레벨 입력.
   - 헷지 모드 필요 시 체크.
4. 매매 시작:
   - 설정 완료 후 '매매 시작' 버튼 클릭.
        """
        self.show_text_window("최초 세팅 방법", setup_text)

    def show_user_manual(self):
        """사용 설명서 표시"""
        manual_text = """
=== 사용 설명서 ===
- Donchian 채널 기반 선물 거래 봇.
- 지원 거래소: OKX, Binance, Bybit (선물 전용).
- 매매 로직: 상단 채널 돌파 시 매수, 하단 이탈 또는 익절 시 매도.
- 헷지 모드: 활성화 시 숏 포지션 가능.
- 설정 저장/불러오기: API 키와 거래 설정을 JSON 파일로 관리.
- 긴급 정지: 모든 포지션 청산 및 미체결 주문 취소.
        """
        self.show_text_window("사용 설명서", manual_text)

    def show_text_window(self, title, text):
        """텍스트 표시 창"""
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("800x600")
        
        text_widget = scrolledtext.ScrolledText(window, wrap=tk.WORD, 
                                               font=('Arial', 10))
        text_widget.pack(fill='both', expand=True, padx=10, pady=10)
        text_widget.insert('1.0', text)
        text_widget.config(state='disabled')

    def get_settings_from_gui(self):
        """GUI에서 설정 읽기"""
        try:
            symbols = []
            for entry in [self.symbol1_entry, self.symbol2_entry, self.symbol3_entry]:
                symbol = entry.get().strip()
                if symbol:
                    if self.exchange_var.get().lower() == 'binance':
                        symbol = symbol.replace(':USDT', '')
                    symbols.append(symbol)
            
            # 현재 GUI 입력값으로 API 키 업데이트
            selected_exchange = self.exchange_var.get().lower()
            self.api_keys[selected_exchange] = {
                'api_key': self.api_key_entry.get(),
                'secret_key': self.secret_key_entry.get(),
                'password': self.password_entry.get()
            }
            
            settings = {
                'exchanges': self.api_keys,
                'selected_exchange': self.exchange_var.get(),
                'symbols': symbols,
                'leverage': int(self.leverage_entry.get()),
                'take_profit_percent': float(self.take_profit_entry.get()),
                'capital_usage_ratio': float(self.capital_usage_entry.get()),
                'donchian_activation_level': int(self.donchian_level_entry.get()),
                'hedge_enabled': self.hedge_var.get(),
                'min_amount': 0.001
            }
            
            return settings
        except ValueError as e:
            raise Exception(f"설정값 오류: {str(e)}")

    def start_trading(self):
            """매매 시작"""
            try:
                settings = self.get_settings_from_gui()
                
                if not all([settings['exchanges'][settings['selected_exchange'].lower()]['api_key'],
                            settings['exchanges'][settings['selected_exchange'].lower()]['secret_key']]) or \
                (settings['selected_exchange'].lower() == 'okx' and not settings['exchanges']['okx']['password']):
                    messagebox.showerror("설정 오류", "API 키 정보를 모두 입력해주세요.")
                    return
                
                if not settings['symbols']:
                    messagebox.showerror("설정 오류", "거래할 종목을 최소 1개 이상 입력해주세요.")
                    return
                
                if settings['donchian_activation_level'] < 1 or settings['donchian_activation_level'] > 10:
                    messagebox.showerror("설정 오류", "손절허용 레벨은 1~10 사이의 값이어야 합니다.")
                    return
                
                if settings['capital_usage_ratio'] > 300:
                    messagebox.showwarning("경고", "자본 사용 비율이 300%를 초과합니다. 위험할 수 있습니다.")
                
                self.trader = MultiSymbolAutoTrader(settings, self.log_queue)
                self.trading_thread = threading.Thread(target=self.trader.run, daemon=False)
                self.trading_thread.start()
                
                self.start_btn.config(state='disabled')
                self.stop_btn.config(state='normal')
                self.emergency_btn.config(state='normal')
                self.status_label.config(text="상태: 매매 중")
                self.log_message("자동매매가 시작되었습니다.")
                
            except Exception as e:
                self.log_message(f"매매 시작 오류: {str(e)}")
                messagebox.showerror("시작 오류", f"매매 시작 중 오류가 발생했습니다:\n{str(e)}")
                # 오류 후 UI 상태 복구
                self.start_btn.config(state='normal')
                self.stop_btn.config(state='disabled')
                self.emergency_btn.config(state='disabled')

    def stop_trading(self):
        """매매 중지"""
        if self.trader:
            self.trader.stop()
            
            if self.trading_thread and self.trading_thread.is_alive():
                self.trading_thread.join(timeout=5)
            
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
            self.emergency_btn.config(state='disabled')
            self.status_label.config(text="상태: 중지됨")
            
            self.log_message("자동매매가 중지되었습니다.")

    def emergency_stop(self):
        """긴급 정지"""
        if self.trader:
            if messagebox.askyesno("긴급 정지", "모든 미체결 주문을 취소하고 포지션을 청산하시겠습니까?"):
                self.trader.emergency_stop()
                
                if self.trading_thread and self.trading_thread.is_alive():
                    self.trading_thread.join(timeout=10)
                
                self.start_btn.config(state='normal')
                self.stop_btn.config(state='disabled')
                self.emergency_btn.config(state='disabled')
                self.status_label.config(text="상태: 긴급 정지됨")
                
                self.log_message("긴급 정지가 실행되었습니다.")

    def update_logs(self):
        """로그 업데이트"""
        try:
            while True:
                message = self.log_queue.get_nowait()
                self.log_text.insert(tk.END, f"{datetime.now().strftime('%H:%M:%S')} {message}\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        
        self.root.after(100, self.update_logs)

    def log_message(self, message):
        """GUI 로그에 메시지 추가"""
        self.log_text.insert(tk.END, f"{datetime.now().strftime('%H:%M:%S')} {message}\n")
        self.log_text.see(tk.END)

    def save_settings(self):
        """설정 저장"""
        try:
            settings = self.get_settings_from_gui()
            
            filename = filedialog.asksaveasfilename(
                defaultextension=".json",
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )
            
            if filename:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(settings, f, indent=2, ensure_ascii=False)
                messagebox.showinfo("저장 완료", "설정이 저장되었습니다.")
                
        except Exception as e:
            messagebox.showerror("저장 오류", f"설정 저장 중 오류가 발생했습니다:\n{str(e)}")

    def load_settings_file(self):
        """설정 파일 불러오기"""
        try:
            filename = filedialog.askopenfilename(
                filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
            )
            
            if filename:
                with open(filename, 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                
                self.load_settings_to_gui(settings)
                messagebox.showinfo("불러오기 완료", "설정이 불러와졌습니다.")
                
        except Exception as e:
            messagebox.showerror("불러오기 오류", f"설정 불러오기 중 오류가 발생했습니다:\n{str(e)}")

    def load_settings(self):
        """기본 설정 불러오기"""
        try:
            if os.path.exists('settings.json'):
                with open('settings.json', 'r', encoding='utf-8') as f:
                    settings = json.load(f)
                self.load_settings_to_gui(settings)
        except Exception as e:
            logger.warning(f"설정 불러오기 실패: {str(e)}")

    def load_settings_to_gui(self, settings):
        """설정을 GUI에 적용"""
        try:
            # API 키 캐시 업데이트
            if 'exchanges' in settings:
                for exchange in ['okx', 'binance', 'bybit']:
                    if exchange in settings['exchanges']:
                        self.api_keys[exchange] = settings['exchanges'][exchange]
            
            # 거래소 선택
            if 'selected_exchange' in settings:
                self.exchange_var.set(settings['selected_exchange'])
            
            # 거래 설정
            if 'symbols' in settings:
                symbols = settings['symbols']
                entries = [self.symbol1_entry, self.symbol2_entry, self.symbol3_entry]
                selected_exchange = self.exchange_var.get().lower()
                for i, entry in enumerate(entries):
                    entry.delete(0, tk.END)
                    if i < len(symbols):
                        symbol = symbols[i]
                        if selected_exchange == 'binance':
                            symbol = symbol.replace(':USDT', '')
                        elif selected_exchange in ['okx', 'bybit'] and not symbol.endswith(':USDT'):
                            symbol = f"{symbol}:USDT"
                        entry.insert(0, symbol)
            
            if 'leverage' in settings:
                self.leverage_entry.delete(0, tk.END)
                self.leverage_entry.insert(0, str(settings['leverage']))
            
            if 'take_profit_percent' in settings:
                self.take_profit_entry.delete(0, tk.END)
                self.take_profit_entry.insert(0, str(settings['take_profit_percent']))
            
            if 'capital_usage_ratio' in settings:
                self.capital_usage_entry.delete(0, tk.END)
                self.capital_usage_entry.insert(0, str(settings['capital_usage_ratio']))
            
            if 'donchian_activation_level' in settings:
                self.donchian_level_entry.delete(0, tk.END)
                self.donchian_level_entry.insert(0, str(settings['donchian_activation_level']))
            
            if 'hedge_enabled' in settings:
                self.hedge_var.set(settings['hedge_enabled'])
            
            # API 키 필드 업데이트
            self.on_exchange_select(None)
            
        except Exception as e:
            logger.warning(f"GUI 설정 적용 실패: {str(e)}")

    def run(self):
        """GUI 실행"""
        try:
            self.root.mainloop()
        finally:
            if hasattr(self, 'trader') and self.trader:
                self.trader.stop()
            if hasattr(self, 'trading_thread') and self.trading_thread and self.trading_thread.is_alive():
                self.trading_thread.join(timeout=3)

def main():
    """메인 함수"""
    try:
        app = TradingGUI()
        app.run()
    except Exception as e:
        logger.error(f"프로그램 실행 오류: {str(e)}")
        messagebox.showerror("실행 오류", f"프로그램 실행 중 오류가 발생했습니다:\n{str(e)}")

if __name__ == "__main__":
    main()
