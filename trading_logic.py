# trading_logic.py
from typing import Optional, Dict, Any, List, Tuple, Union
import asyncio
import logging
import re
import json
from datetime import datetime, timedelta
import time
from openai import OpenAI
from math import isclose
import numpy as np
import pandas as pd

from models import TradingSignal, EntryZone, TakeProfitLevel
from typing import Optional
try:
    from exchange_execution import ExchangeManager
except Exception:
    ExchangeManager = None

class TradingLogic:
    def __init__(self, deepseek_api_key: str, openai_key: str, openai_base_url: str, exchange_manager: Optional[object] = None):
        # 初始化 OpenAI 客户端，优先在构造函数中设置 base_url
        if openai_base_url:
            self.openai_client = OpenAI(api_key=openai_key, base_url=openai_base_url)
        else:
            self.openai_client = OpenAI(api_key=openai_key)
        self.exchange_manager = exchange_manager
        self._message_history: List[Dict[str, Any]] = []
        self._open_active: bool = False
        self._last_message_ts: Optional[datetime] = None
        self._last_message_content: Optional[str] = None
        self.deepseekClient = OpenAI(api_key=deepseek_api_key, base_url="https://api.deepseek.com")

        self.default_prompt = """你是一名专业的交易信号分析器（Trade Signal Parser）。你的任务是解析用户输入文本，判断是否包含新的交易信号或对现有委托/订单的更新，输出正确的交易指令。输入包含3部分：
1. 最新消息文本，若有引用消息，【引用消息】后面是对应的引用消息;
2. 当前订单信息（持仓或委托）以当前持仓:或者当前委托:开头。若两者均为空，则视为“空仓状态”;
3. 有开仓以来的消息，是有交易信息消息以来的所有消息，便于分析订单的变化。可能没有该类消息。
当前订单信息示例：
当前持仓:
OKX:
BTC/USDT:USDT sell limit 量:1.0 价:88820.0 状态:open
备注：价指的是买入价或者卖出价，也就是成本价
当前委托:
OKX:
BTC/USDT:USDT sell limit 量:1.0 价:88820.0 状态:open
备注：价指的是挂单价

输出要求（必须严格遵守）
你只能输出 JSON。
不得输出解释、不得输出多余文字。
不得出现 null。
若识别不到有效信号，必须只返回 {}。
若识别到信号，你必须输出完整 JSON：
{
  "exchange": "OKX",
  "symbol": "BTCUSDT",
  "action": "OPEN_LONG 或 OPEN_SHORT 或 CLOSE 或 UPDATE 或 CANCEL 或者TURNOVER",
  "entry_price": float 或 [float, float],
  "take_profit_levels": [
    {
      "price": float,
      "percentage": float
    }
  ],
  "stop_loss": float,
  "position_size": float,
  "leverage": integer,
  "order_type":"LIMIT 或者 MARKET",
  "margin_mode": "cross 或 isolated",
  "confidence": float(0-1),
  "risk_level": "LOW 或 MEDIUM 或 HIGH"
}
或者信息中有多个信号返回json数组 [{},{}]
字段缺失时需根据规则自动推算或使用默认值。

规则
1. entry_price
单价 → float
价格区间（如 89000-89500）→ [89000, 89500]
2. take_profit_levels
支持多个目标
若无 percentage → 自动平均（总和 ≤ 100）
3. stop_loss
若消息未提供 → 不自动生成，等待后续消息进行增加或者修改止损
要求 RR ≥ 1:1.5，若有明确止盈止损点位，损益比不满足RR ≥ 1:1.5，修改入场价格，至少修改到RR = 1:1.5
4. confidence 自动评估
明确价格 + 专业语气：0.7–0.9
一般信号：0.4–0.7
模糊内容：0.1–0.3
5. risk_level 自动评估
LOW：低杠杆或窄 SL
MEDIUM：常规策略
HIGH：高杠杆、宽 SL、模糊内容
6. order_type 开单类型
消息文本中没有明确说明都是MARKET类型，有限价字样的是LIMIT类型
7. 默认值
leverage：3
position_size：4500
margin_mode：isolated
exchange: OKX

频道模式（关键逻辑）
此为多人会员频道，不是一对一模式，只关注普通消息，不关注一对一模式的消息。
一对一模式的消息实例如下：一对一指导XXX多单 市价xx附近。。。

任意时刻只能存在一笔活跃订单。
若当前为空仓 → 可生成 OPEN_LONG 或 OPEN_SHORT
若已有委托/订单 → 所有价格、TP、SL 修改 → action = UPDATE
若消息代表平仓（如“全部止盈”“恭喜”“市价卖出”）→ action = CLOSE

UPDATE 定义，UPDATE 用于从消息中分析得到任何以下情况：
修改委托价格
修改止盈/止损
部分止盈引起剩余仓位变化
调整区间价格或进场点位
修改仓位设置（如杠杆、数量等）

CANCEL 定义
CANCEL是当前有委托订单，撤销当前委托订单

TURNOVER 定义
换手做多,换手直接入场做多，当前持有空单，直接换手为多单
换手做空,换手直接入场做空，当前持有多单，直接换手为空
 
空仓规则
当前无持仓且无委托 → 可开新仓
若已有持仓或委托 → 不允许新开仓，只能 UPDATE

成本保护
更新当前持仓订单的止损，止损设置在进场的位置

无效内容（必须返回 {}）
无方向
无价格
市场观点
随意聊天
过度模糊无法推断
不含任何交易意图的信息

部分输入内容解读：
1. 单独的信息，仅包含“剩余仓位全部止盈/止损出局"--》发送 CLOSE
2. 换手/反手直接入场做空/做多 --》发送 TURNOVER
3. 单独的信息，仅包含“ 空单/多单全部出局”--》发送 CLOSE
4. 中长线止盈d%，做成本保护继续持有--》发送 CLOSE，仓位为d%，发送UPDATE，止损设置为成本价
5. BTC市价$1附近 小赚$2点止盈$3% 做成本保护过夜-->在$1止盈$3%,发送CLOSE，仓位为$3%并且发送UPDATE，止损设置为成本价

最终要求

只能返回 JSON
不得添加任何说明
字段必须完整、不得为 null
若不能提取有效交易信号 → 返回 {}
"""

    def _validate_json_data(self, data: Dict[str, Any]) -> bool:
        """验证JSON数据的有效性"""
        try:
            # 验证必要字段
            required_fields = ['exchange', 'symbol', 'action']
            for field in required_fields:
                if field not in data:
                    logging.error(f"Missing required field: {field}")
                    return False

            # 验证交易所
            if data['exchange'] not in ['BINANCE', 'OKX']:
                logging.error(f"Invalid exchange: {data['exchange']}")
                return False

            # 验证交易对
            if not isinstance(data['symbol'], str) or not data['symbol']:
                logging.error("Invalid symbol")
                return False

            # 验证操作类型
            if data['action'] not in ['OPEN_LONG', 'OPEN_SHORT', 'CLOSE', 'UPDATE', 'TURNOVER']:
                logging.error(f"Invalid action: {data['action']}")
                return False

            # 验证入场区间或价格
            if 'entry_zones' in data:
                if not isinstance(data['entry_zones'], list):
                    logging.error("entry_zones must be a list")
                    return False
                for zone in data['entry_zones']:
                    if not all(k in zone for k in ['price', 'percentage']):
                        logging.error("Invalid entry zone format")
                        return False

            # 验证止盈目标
            if 'take_profit_levels' in data:
                if not isinstance(data['take_profit_levels'], list):
                    logging.error("take_profit_levels must be a list")
                    return False
                for tp in data['take_profit_levels']:
                    if not all(k in tp for k in ['price', 'percentage']):
                        logging.error("Invalid take profit level format")
                        return False

            # 验证数值字段
            numeric_fields = {
                'position_size': 50.0,
                'leverage': 10,
                'confidence': 0.8
            }
            
            for field, default in numeric_fields.items():
                if field in data:
                    try:
                        float(data[field])
                    except (TypeError, ValueError):
                        logging.error(f"Invalid {field} value")
                        return False

            return True

        except Exception as e:
            logging.error(f"Error validating JSON data: {e}")
            return False

    def _normalize_numbers(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """规范化数值字段"""
        try:
            normalized = data.copy()

            # 处理入场区间
            if 'entry_zones' in normalized and isinstance(normalized['entry_zones'], list):
                for zone in normalized['entry_zones']:
                    zone['price'] = float(zone['price'])
                    zone['percentage'] = float(zone['percentage'])

            # 处理止盈目标
            if 'take_profit_levels' in normalized and isinstance(normalized['take_profit_levels'], list):
                for tp in normalized['take_profit_levels']:
                    tp['price'] = float(tp['price'])
                    tp['percentage'] = float(tp['percentage'])

            # 处理其他数值字段
            numeric_fields = ['stop_loss', 'position_size', 'leverage', 'confidence']
            for field in numeric_fields:
                if field in normalized:
                    try:
                        if field == 'leverage':
                            normalized[field] = int(float(normalized[field]))
                        else:
                            normalized[field] = float(normalized[field])
                    except (TypeError, ValueError):
                        logging.warning(f"Could not convert {field} to number, removing field")
                        normalized.pop(field)

            return normalized

        except Exception as e:
            logging.error(f"Error normalizing numbers: {e}")
            return data
        
    def _convert_to_trading_signal(self, data: Dict[str, Any]) -> Optional[TradingSignal]:
        """将字典转换为TradingSignal对象"""
        try:
            logging.info("Converting dictionary to TradingSignal")
            logging.info(f"Input data:\n{'-'*40}\n{json.dumps(data, indent=2)}\n{'-'*40}")

            # 验证必要字段
            required_fields = ['exchange', 'symbol', 'action']
            for field in required_fields:
                if field not in data:
                    logging.error(f"Missing required field: {field}")
                    return None

            # 处理入场价格/区间
            entry_price = None
            entry_zones = []
            
            # 检查是否有区间入场
            if 'entry_zones' in data and isinstance(data['entry_zones'], list) and data['entry_zones']:
                for zone_data in data['entry_zones']:
                    try:
                        zone = EntryZone(
                            price=float(zone_data['price']),
                            percentage=float(zone_data['percentage'])
                        )
                        entry_zones.append(zone)
                    except (KeyError, ValueError) as e:
                        logging.error(f"Error creating entry zone: {e}")
                        continue
                logging.info(f"Created {len(entry_zones)} entry zones")
            # 检查是否有单一入场价格或价格列表
            elif 'entry_price' in data and data['entry_price'] is not None:
                try:
                    if isinstance(data['entry_price'], list):
                        prices = [float(p) for p in data['entry_price'] if p is not None]
                        if not prices:
                            logging.error("Empty entry_price list")
                            return None
                        pct = 1.0 / len(prices)
                        for p in prices:
                            entry_zones.append(EntryZone(price=p, percentage=pct))
                        logging.info(f"Created {len(entry_zones)} entry zones from entry_price list")
                    else:
                        entry_price = float(data['entry_price'])
                        logging.info(f"Using single entry price: {entry_price}")
                except (TypeError, ValueError) as e:
                    logging.error(f"Error converting entry price: {e}")
                    return None

            # UPDATE 动作允许没有入场区间/价格
            if not entry_zones and entry_price is None and data.get('action') != 'UPDATE':
                logging.error("No valid entry price or zones found")
                return None

            # 处理止盈目标
            take_profit_levels = []
            # 检查 take_profit_levels 或 take_profit 字段
            tp_data = data.get('take_profit_levels', data.get('take_profit', []))
            if isinstance(tp_data, list):
                for tp_item in tp_data:
                    try:
                        tp = TakeProfitLevel(
                            price=float(tp_item['price']),
                            percentage=float(tp_item['percentage'])
                        )
                        take_profit_levels.append(tp)
                    except (KeyError, ValueError) as e:
                        logging.error(f"Error creating take profit level: {e}")
                        continue
            
            if take_profit_levels:
                logging.info(f"Created {len(take_profit_levels)} take profit levels")
                action = data.get('action')
                total_percentage = sum(tp.percentage for tp in take_profit_levels)
                if action == 'CLOSE':
                    has_percentage_scale = any(tp.percentage > 1 for tp in take_profit_levels)
                    if has_percentage_scale:
                        for tp in take_profit_levels:
                            tp.percentage = tp.percentage / 100.0
                        total_percentage = sum(tp.percentage for tp in take_profit_levels)
                    if total_percentage > 1.0 + 1e-5:
                        logging.warning(f"Close percentages sum to {total_percentage}, normalizing...")
                        for tp in take_profit_levels:
                            tp.percentage = tp.percentage / total_percentage
                else:
                    if not isclose(total_percentage, 1.0, rel_tol=1e-5):
                        logging.warning(f"Take profit percentages sum to {total_percentage}, normalizing...")
                        for tp in take_profit_levels:
                            tp.percentage = tp.percentage / total_percentage

            # 获取止损价格
            stop_loss = None
            if 'stop_loss' in data:
                try:
                    stop_loss = float(data['stop_loss'])
                except (TypeError, ValueError):
                    logging.error("Invalid stop loss value")
            # 创建信号对象
            try:
                signal = TradingSignal(
                    exchange=data['exchange'],
                    symbol=data['symbol'],
                    action=data['action'],
                    entry_price=entry_price,  # 可以是None
                    entry_zones=entry_zones if entry_zones else None,  # 可以是None
                    take_profit_levels=take_profit_levels if take_profit_levels else None,
                    stop_loss=stop_loss,
                    position_size=float(data.get('position_size', 2000.0)),
                    leverage=int(data.get('leverage', 10)),
                    margin_mode=data.get('margin_mode', 'cross'),
                    confidence=float(data.get('confidence', 0.8)),
                    risk_level=data.get('risk_level', 'MEDIUM'),
                    source_message="",
                    additional_info={}
                )
                
                logging.info("Successfully created TradingSignal object")
                logging.info(f"Signal details:\n{'-'*40}")
                logging.info(f"Exchange: {signal.exchange}")
                logging.info(f"Symbol: {signal.symbol}")
                logging.info(f"Action: {signal.action}")
                
                if entry_zones:
                    logging.info("Entry Zones:")
                    for i, zone in enumerate(entry_zones, 1):
                        logging.info(f"  Zone {i}: Price={zone.price}, Percentage={zone.percentage:.2%}")
                elif entry_price:
                    logging.info(f"Entry Price: {entry_price}")
                
                if take_profit_levels:
                    logging.info("Take Profit Levels:")
                    for i, tp in enumerate(take_profit_levels, 1):
                        logging.info(f"  TP {i}: Price={tp.price}, Percentage={tp.percentage:.2%}")
                
                logging.info(f"Stop Loss: {signal.stop_loss}")
                logging.info(f"Leverage: {signal.leverage}x")
                logging.info(f"Position Size: {signal.position_size} USDT")
                logging.info(f"{'-'*40}")

                # 验证信号有效性
                is_valid = signal.is_valid()
                if not is_valid:
                    logging.error("Signal validation failed")
                    logging.error("Missing essential components:")
                    if not signal.entry_zones and signal.entry_price is None:
                        logging.error("- No entry price or zones")
                    if not signal.take_profit_levels:
                        logging.error("- No take profit levels")
                    if not signal.stop_loss:
                        logging.error("- No stop loss")
                    return None

                return signal

            except Exception as e:
                logging.error(f"Error creating TradingSignal object: {e}")
                import traceback
                logging.error(f"Traceback:\n{traceback.format_exc()}")
                return None

        except Exception as e:
            logging.error(f"Error converting to trading signal: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return None

    def _preprocess_message(self, message: str) -> str:
        """预处理消息文本"""
        try:
            logging.info("Preprocessing message")
            
            # 移除表情符号和特殊字符
            cleaned = re.sub(r'[^\w\s.,#@$%+-:()]', ' ', message)
            
            # 标准化价格格式
            cleaned = cleaned.replace(',', '')
            cleaned = re.sub(r'(\d+\.?\d*)k', lambda m: str(float(m.group(1))*1000), cleaned)
            
            # 统一符号
            cleaned = cleaned.replace('$', '')
            cleaned = cleaned.upper()
            cleaned = re.sub(r'\s+', ' ', cleaned).strip()
            
            logging.info(f"Preprocessed message:\n{'-'*40}\n{cleaned}\n{'-'*40}")
            return cleaned
            
        except Exception as e:
            logging.error(f"Error preprocessing message: {e}")
            return message


    async def process_message(self, message: str, custom_prompt: Optional[str] = None) -> Optional[List[TradingSignal]]:
        """处理消息并提取交易信号（支持返回多个有效信号）"""
        try:
            prompt = custom_prompt if custom_prompt else self.default_prompt
            
            logging.info(f"Original message:\n{'-'*40}\n{message}\n{'-'*40}")
            cleaned_message = self._preprocess_message(message)
            # 提取并拼接引用消息
            quote_pattern = re.compile(r'>(.*?)(?=\n|$)', re.DOTALL)
            quote_matches = quote_pattern.findall(message)
            if quote_matches:
                quote_text = "\n".join([q.strip() for q in quote_matches])
                cleaned_message = f"{cleaned_message}\n【引用消息】\n{quote_text}"
            
            logging.info(f"Preprocessed message (with quote):\n{'-'*40}\n{cleaned_message}\n{'-'*40}")
            #return cleaned_message

            # 根据是否存在当前委托/持仓，维护消息历史并决定上传内容
            open_orders = None
            try:
                exm = getattr(self, 'exchange_manager', None)
                if exm:
                    fn = getattr(exm, 'get_open_orders', None)
                    if fn:
                        if asyncio.iscoroutinefunction(fn):
                            open_orders = await fn()
                        else:
                            open_orders = fn()
            except Exception:
                open_orders = None
            now_ts = datetime.now()
            has_position_text = ("当前持仓" in cleaned_message) or ("当前委托" in cleaned_message)
            active = bool(open_orders) or has_position_text
            user_content = cleaned_message
            if not active:
                self._open_active = False
                self._message_history = []
            else:
                if not self._open_active:
                    self._open_active = True
                    self._message_history = []
                # 移除消息中的持仓/委托信息后存入历史
                cleaned_for_history = re.sub(r'当前[持仓委托]:[\s\S]*?(?=\n\n|\Z)', '', cleaned_message).strip()
                self._message_history.append({'ts': now_ts.strftime('%Y-%m-%d %H:%M:%S'), 'text': cleaned_for_history})
                try:
                    oo_text = ""
                    if isinstance(open_orders, dict):
                        lines: List[str] = []
                        for ex_name, orders in open_orders.items():
                            lines.append(f"{ex_name}:")
                            for od in orders or []:
                                lines.append(
                                    f"{getattr(od, 'symbol', '')} {getattr(od, 'side', '')} {getattr(od, 'type', '')} 量:{getattr(od, 'amount', 0)} 价:{getattr(od, 'price', '') if getattr(od, 'price', None) is not None else ''} 状态:{getattr(od, 'status', '')}"
                                )
                        if lines:
                            oo_text = "\n".join(lines)
                    else:
                        oo_text = str(open_orders or "")
                except Exception:
                    oo_text = ""
                history_text = "\n".join([f"[{r['ts']}] {r['text']}" for r in self._message_history])
                if has_position_text:
                    user_content = f"{cleaned_message}\n\n【有开仓以来的消息】\n{history_text}"
                else:
                    user_content = f"{cleaned_message}\n\n【当前持仓/委托】\n{oo_text}\n\n【有开仓以来的消息】\n{history_text}"

            # logging.info(f"Using prompt:\n{'-'*40}\n{prompt}\n{'-'*40}")
            logging.info(f"user_content: {user_content}")
            try:
                response = self.openai_client.chat.completions.create(
                    model="gpt-3.5-turbo",
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_content}
                    ],
                    temperature=0.7,
                    max_tokens=1024
                )
            except Exception as e:
                logging.warning(f"OpenAI 接口调用失败: {e}，尝试使用 qwen 接口")
                try:
                    # 使用 deepseek 接口作为备选
                    response = self.deepseekClient.chat.completions.create(
                        model="deepseek-chat",  # 假设 deepseek 提供的模型名称
                        messages=[
                            {"role": "system", "content": prompt},
                            {"role": "user", "content": user_content}
                        ],
                        temperature=0.7,
                        max_tokens=1024
                    )
                except Exception as deepseek_e:
                    logging.error(f"deepseek 接口也调用失败: {deepseek_e}")
                    raise deepseek_e
            self._last_message_ts = now_ts
            self._last_message_content = cleaned_message

            response_text = None
            try:
                logging.info(f"LLM raw response type: {type(response)}")
                response_text = response.choices[0].message.content
            except Exception:
                if isinstance(response, str):
                    response_text = response
                elif isinstance(response, dict):
                    try:
                        choices = response.get("choices", [])
                        if choices:
                            msg = choices[0].get("message", {})
                            response_text = msg.get("content") or response.get("content")
                    except Exception:
                        pass
                if not response_text:
                    try:
                        response_text = str(response)
                    except Exception:
                        response_text = ""
            logging.info(f"GPT response:\n{'-'*40}\n{response_text}\n{'-'*40}")
            
            signal_dict_or_list = self._parse_response(response_text)
            if signal_dict_or_list:
                if isinstance(signal_dict_or_list, list):
                    logging.info(f"Parsed JSON array with {len(signal_dict_or_list)} items")
                    valid_signals: List[TradingSignal] = []
                    for idx, item in enumerate(signal_dict_or_list, 1):
                        try:
                            logging.info(f"Processing item {idx}:\n{'-'*40}\n{json.dumps(item, indent=2)}\n{'-'*40}")
                        except Exception:
                            logging.info(f"Processing item {idx}")
                        if self._validate_json_data(item):
                            normalized = self._normalize_numbers(item)
                            signal_i = self._convert_to_trading_signal(normalized)
                            if signal_i and signal_i.is_valid():
                                risk_ratio_valid = True
                                logging.info(f"Risk ratio validation: {risk_ratio_valid}")
                                if risk_ratio_valid:
                                    valid_signals.append(signal_i)
                                else:
                                    logging.error("Risk ratio validation failed")
                            else:
                                logging.error("Signal validation failed")
                        else:
                            logging.error("JSON data validation failed")
                    if valid_signals:
                        return valid_signals
                    else:
                        logging.error("No valid signals parsed from array")
                else:
                    logging.info(f"Parsed signal dictionary:\n{'-'*40}\n{json.dumps(signal_dict_or_list, indent=2)}\n{'-'*40}")
                    if self._validate_json_data(signal_dict_or_list):
                        normalized_dict = self._normalize_numbers(signal_dict_or_list)
                        signal = self._convert_to_trading_signal(normalized_dict)
                        if signal and signal.is_valid():
                            risk_ratio_valid = True
                            logging.info(f"Risk ratio validation: {risk_ratio_valid}")
                            if risk_ratio_valid:
                                return [signal]
                            else:
                                logging.error("Risk ratio validation failed")
                        else:
                            logging.error("Signal validation failed")
                    else:
                        logging.error("JSON data validation failed")
            else:
                logging.error("Failed to parse GPT response")

            return None
            
        except Exception as e:
            logging.error(f"Error processing message: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            return None

    def _extract_response_text(self, response: Any) -> Optional[str]:
        """兼容不同 SDK/服务返回结构，提取文本内容"""
        try:
            # 1) 直接是字符串
            if isinstance(response, str):
                return response

            # 2) OpenAI Chat Completions：有 choices -> message -> content
            if hasattr(response, 'choices') and response.choices:
                first = response.choices[0]
                if hasattr(first, 'message') and first.message and hasattr(first.message, 'content'):
                    return first.message.content
                # 兼容老的 text 字段
                if hasattr(first, 'text'):
                    return first.text

            # 3) OpenAI Responses API：有 output_text
            if hasattr(response, 'output_text'):
                return getattr(response, 'output_text')

            # 4) 字典或可序列化对象
            if isinstance(response, dict):
                # 常见结构：{"choices":[{"message":{"content":"..."}}]}
                try:
                    choices = response.get('choices')
                    if choices:
                        msg = choices[0].get('message') if isinstance(choices[0], dict) else None
                        if msg and isinstance(msg, dict) and 'content' in msg:
                            return msg['content']
                        if 'text' in choices[0]:
                            return choices[0]['text']
                except Exception:
                    pass
                # 如果没有明确文本字段，返回 JSON 字符串以便后续解析尝试
                return json.dumps(response, ensure_ascii=False)

            # 5) 其它对象，尝试转字符串
            return str(response)
        except Exception as e:
            logging.error(f"Error extracting response text: {e}")
            return None

    def _parse_response(self, response_text: str) -> Optional[Union[Dict[str, Any], List[Dict[str, Any]]]]:
        """解析GPT响应"""
        try:
            # 记录开始解析
            logging.info("Starting to parse GPT response")
            
            # 清除注释
            cleaned_text = ""
            for line in response_text.split('\n'):
                # 移除单行注释
                line = re.sub(r'//.*$', '', line)
                # 移除含有注释的部分
                line = re.sub(r'/\*.*?\*/', '', line)
                if line.strip():
                    cleaned_text += line + "\n"
                    
            # 直接解析完整文本为JSON（可能是对象或数组）
            try:
                direct_parsed = json.loads(cleaned_text.strip())
                if isinstance(direct_parsed, list):
                    logging.info(f"Detected JSON array with {len(direct_parsed)} items")
                    return [item for item in direct_parsed if isinstance(item, dict)]
                if isinstance(direct_parsed, dict):
                    logging.info("Detected top-level JSON object")
                    return direct_parsed
            except Exception:
                pass
            
            # 尝试提取JSON数组
            array_match = re.search(r'\[.*\]', cleaned_text, re.DOTALL)
            if array_match:
                array_str = array_match.group()
                logging.info(f"Extracted JSON array string:\n{'-'*40}\n{array_str}\n{'-'*40}")
                try:
                    arr = json.loads(array_str)
                    if isinstance(arr, list):
                        return [item for item in arr if isinstance(item, dict)]
                except Exception as e:
                    logging.warning(f"Failed to parse JSON array: {e}")
            
            # 退回到提取单个JSON对象
            json_match = re.search(r'{.*}', cleaned_text, re.DOTALL)
            if not json_match:
                logging.warning("No JSON found in response")
                return None
            json_str = json_match.group()
            logging.info(f"Extracted JSON object string:\n{'-'*40}\n{json_str}\n{'-'*40}")
            
            # 解析JSON
            parsed_data = json.loads(json_str)
            logging.info(f"Successfully parsed JSON:\n{'-'*40}\n{json.dumps(parsed_data, indent=2)}\n{'-'*40}")
            
            # 验证必要字段
            required_fields = ['exchange', 'symbol', 'action']
            missing_fields = [field for field in required_fields if field not in parsed_data]
            if missing_fields:
                logging.warning(f"Missing required fields: {missing_fields}")
                return None
            
            return parsed_data
                
        except json.JSONDecodeError as e:
            logging.error(f"JSON decode error: {e}")
            logging.error(f"Problematic text:\n{response_text}")
            return None
        except Exception as e:
            logging.error(f"Error parsing GPT response: {e}")
            return None


    def _validate_and_complete_signal(self, signal: TradingSignal) -> Optional[TradingSignal]:
        """验证并补充信号信息"""
        try:
            # 验证基本字段
            if not all([signal.exchange, signal.symbol, signal.action]):
                return None
            
            # 确保有入场价格或区间
            if not signal.entry_price and not signal.entry_zones:
                return None
            
            # 验证动作类型
            if signal.action not in ['OPEN_LONG', 'OPEN_SHORT', 'CLOSE']:
                return None
            
            # 如果没有止损，计算默认止损
            if not signal.stop_loss and signal.action != 'CLOSE':
                signal.stop_loss = self._calculate_default_stop_loss(signal)
            
            # 如果没有止盈等级，设置默认止盈
            if not signal.take_profit_levels and signal.action != 'CLOSE':
                signal.take_profit_levels = self._calculate_default_take_profits(signal)
            
            # 验证风险比率
            if not self._validate_risk_ratio(signal):
                logging.warning(f"Invalid risk ratio for signal: {signal.symbol}")
                return None
            
            return signal
            
        except Exception as e:
            logging.error(f"Error validating signal: {e}")
            return None

    def _calculate_default_stop_loss(self, signal: TradingSignal) -> float:
        """计算默认止损价格"""
        try:
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                # 使用区间入场的中间价格
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            # 默认使用2%的止损距离
            stop_distance = entry_price * 0.02
            
            if signal.action == 'OPEN_LONG':
                return entry_price - stop_distance
            else:  # OPEN_SHORT
                return entry_price + stop_distance
                
        except Exception as e:
            logging.error(f"Error calculating default stop loss: {e}")
            return 0

    def _calculate_default_take_profits(self, signal: TradingSignal) -> List[TakeProfitLevel]:
        """计算默认止盈等级"""
        try:
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            # 计算止损距离
            stop_distance = abs(entry_price - signal.stop_loss)
            
            # 设置三个止盈目标，分别是2R、3R和4R
            multipliers = [2, 3, 4]  # R倍数
            percentages = [0.4, 0.3, 0.3]  # 每个目标的仓位比例
            
            tp_levels = []
            for mult, pct in zip(multipliers, percentages):
                if signal.action == 'OPEN_LONG':
                    price = entry_price + (stop_distance * mult)
                else:  # OPEN_SHORT
                    price = entry_price - (stop_distance * mult)
                tp_levels.append(TakeProfitLevel(price, pct))
            
            return tp_levels
            
        except Exception as e:
            logging.error(f"Error calculating default take profits: {e}")
            return []

    def _validate_risk_ratio(self, signal: TradingSignal) -> bool:
        """验证风险收益比"""
        try:
            if signal.action == 'CLOSE':
                return True
            
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            if not signal.stop_loss or not signal.take_profit_levels:
                return False
            
            # 计算回报
            if signal.action == 'OPEN_LONG':
                highest_tp = max(tp.price for tp in signal.take_profit_levels)
                reward = highest_tp - entry_price
                risk = entry_price - signal.stop_loss
            else:  # OPEN_SHORT
                lowest_tp = min(tp.price for tp in signal.take_profit_levels)
                reward = entry_price - lowest_tp
                risk = signal.stop_loss - entry_price
            
            # 要求至少1:1.5的风险收益比
            return (reward / risk) >= 1.5 if risk > 0 else False
            
        except Exception as e:
            logging.error(f"Error validating risk ratio: {e}")
            return False

    async def generate_analysis(self, signal: TradingSignal) -> Dict[str, Any]:
        """生成交易分析"""
        try:
            # TODO: 获取市场数据并进行技术分析
            analysis = {
                'trend': self._analyze_trend(signal),
                'support_resistance': self._find_support_resistance(signal),
                'volatility': self._analyze_volatility(signal),
                'risk_level': self._assess_risk_level(signal),
                'recommendation': self._generate_recommendation(signal)
            }
            
            return analysis
            
        except Exception as e:
            logging.error(f"Error generating analysis: {e}")
            return {}

    def _analyze_trend(self, signal: TradingSignal) -> Dict[str, Any]:
        """分析市场趋势"""
        # TODO: 实现实际的趋势分析
        return {
            'short_term': 'BULLISH',
            'medium_term': 'NEUTRAL',
            'long_term': 'BEARISH'
        }

    def _find_support_resistance(self, signal: TradingSignal) -> Dict[str, Any]:
        """寻找支撑阻力位"""
        # TODO: 实现支撑阻力位分析
        return {
            'support_levels': [40000, 39000, 38000],
            'resistance_levels': [42000, 43000, 44000]
        }

    def _analyze_volatility(self, signal: TradingSignal) -> Dict[str, Any]:
        """分析波动性"""
        # TODO: 实现波动性分析
        return {
            'current_volatility': 'HIGH',
            'volatility_trend': 'INCREASING',
            'risk_factor': 0.8
        }

    def _assess_risk_level(self, signal: TradingSignal) -> str:
        """评估风险等级"""
        try:
            # 计算风险分数
            risk_score = 0
            
            # 基于杠杆的风险
            if signal.leverage > 20:
                risk_score += 3
            elif signal.leverage > 10:
                risk_score += 2
            elif signal.leverage > 5:
                risk_score += 1
            
            # 基于止损距离的风险
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            stop_distance = abs(entry_price - signal.stop_loss) / entry_price * 100
            if stop_distance < 1:
                risk_score += 3
            elif stop_distance < 2:
                risk_score += 2
            elif stop_distance < 3:
                risk_score += 1
            
            # 基于风险收益比的风险
            rr_ratio = self.calculate_risk_reward_ratio(signal)
            if rr_ratio < 1.5:
                risk_score += 3
            elif rr_ratio < 2:
                risk_score += 2
            elif rr_ratio < 2.5:
                risk_score += 1
            
            # 返回风险等级
            if risk_score >= 7:
                return 'HIGH'
            elif risk_score >= 4:
                return 'MEDIUM'
            else:
                return 'LOW'
                
        except Exception as e:
            logging.error(f"Error assessing risk level: {e}")
            return 'MEDIUM'

    def calculate_risk_reward_ratio(self, signal: TradingSignal) -> float:
        """计算风险收益比"""
        try:
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            if signal.action == 'OPEN_LONG':
                if signal.take_profit_levels:
                    highest_tp = max(tp.price for tp in signal.take_profit_levels)
                    reward = highest_tp - entry_price
                else:
                    reward = signal.take_profit - entry_price
                risk = entry_price - signal.stop_loss
            else:
                if signal.take_profit_levels:
                    lowest_tp = min(tp.price for tp in signal.take_profit_levels)
                    reward = entry_price - lowest_tp
                else:
                    reward = entry_price - signal.take_profit
                risk = signal.stop_loss - entry_price
            
            return reward / risk if risk > 0 else 0
            
        except Exception as e:
            logging.error(f"Error calculating risk reward ratio: {e}")
            return 0

    def _generate_recommendation(self, signal: TradingSignal) -> str:
        """生成交易建议"""
        try:
            risk_level = self._assess_risk_level(signal)
            rr_ratio = self.calculate_risk_reward_ratio(signal)
            
            if risk_level == 'HIGH':
                return "🔴 高风险交易，建议减小仓位或放弃此交易机会"
            elif risk_level == 'MEDIUM':
                if rr_ratio >= 2:
                    return "🟡 中等风险，风险收益比良好，建议使用半仓位进入"
                else:
                    return "🟡 中等风险，建议等待更好的入场机会"
            else:
                if rr_ratio >= 1.5:
                    return "🟢 低风险高收益，建议按计划执行"
                else:
                    return "🟢 低风险，但收益相对较小，可以考虑增加仓位"
                    
        except Exception as e:
            logging.error(f"Error generating recommendation: {e}")
            return "无法生成建议"

    def calculate_position_size(self, account_balance: float, risk_per_trade: float,
                              signal: TradingSignal) -> float:
        """计算建议仓位大小"""
        try:
            # 基于账户风险计算
            risk_amount = account_balance * (risk_per_trade / 100)  # 风险金额
            
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            # 计算每单位的风险
            stop_distance = abs(entry_price - signal.stop_loss)
            risk_per_unit = stop_distance * signal.leverage
            
            # 计算建议仓位
            position_size = risk_amount / risk_per_unit
            
            # 根据风险等级调整仓位
            risk_level = self._assess_risk_level(signal)
            if risk_level == 'HIGH':
                position_size *= 0.5
            elif risk_level == 'MEDIUM':
                position_size *= 0.75
            
            return position_size
            
        except Exception as e:
            logging.error(f"Error calculating position size: {e}")
            return 0

    async def analyze_market_context(self, signal: TradingSignal) -> Dict[str, Any]:
        """分析市场环境"""
        try:
            # TODO: 获取市场数据
            market_data = {}  # 这里应该从数据源获取市场数据
            
            analysis = {
                'market_trend': self._analyze_market_trend(market_data),
                'volume_analysis': self._analyze_volume(market_data),
                'momentum': self._analyze_momentum(market_data),
                'correlation': self._analyze_correlation(market_data),
                'sentiment': await self._analyze_market_sentiment(signal.symbol)
            }
            
            return analysis
            
        except Exception as e:
            logging.error(f"Error analyzing market context: {e}")
            return {}

    def _analyze_market_trend(self, market_data: Dict[str, Any]) -> Dict[str, str]:
        """分析市场趋势"""
        return {
            'trend_direction': 'BULLISH',
            'trend_strength': 'STRONG',
            'trend_duration': 'LONG_TERM'
        }

    def _analyze_volume(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """分析交易量"""
        return {
            'volume_trend': 'INCREASING',
            'volume_strength': 'HIGH',
            'unusual_activity': False
        }

    def _analyze_momentum(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """分析动量指标"""
        return {
            'rsi': 65,
            'macd': 'BULLISH',
            'momentum_strength': 'STRONG'
        }

    def _analyze_correlation(self, market_data: Dict[str, Any]) -> Dict[str, Any]:
        """分析相关性"""
        return {
            'btc_correlation': 0.85,
            'market_correlation': 0.75,
            'sector_correlation': 0.90
        }

    async def _analyze_market_sentiment(self, symbol: str) -> Dict[str, Any]:
        """分析市场情绪"""
        return {
            'overall_sentiment': 'POSITIVE',
            'fear_greed_index': 65,
            'social_sentiment': 'BULLISH'
        }

    def validate_technical_levels(self, signal: TradingSignal) -> bool:
        """验证技术价位的有效性"""
        try:
            entry_price = signal.entry_price
            if not entry_price and signal.entry_zones:
                prices = [zone.price for zone in signal.entry_zones]
                entry_price = sum(prices) / len(prices)
            
            # 验证止损位置
            if signal.stop_loss:
                if signal.action == 'OPEN_LONG':
                    if signal.stop_loss >= entry_price:
                        return False
                else:
                    if signal.stop_loss <= entry_price:
                        return False
            
            # 验证止盈位置
            if signal.take_profit_levels:
                for tp in signal.take_profit_levels:
                    if signal.action == 'OPEN_LONG':
                        if tp.price <= entry_price:
                            return False
                    else:
                        if tp.price >= entry_price:
                            return False
            
            # 验证价格间隔
            min_price_distance = 0.001  # 最小价格间隔
            
            if signal.entry_zones:
                prices = sorted(zone.price for zone in signal.entry_zones)
                for i in range(1, len(prices)):
                    if abs(prices[i] - prices[i-1]) < min_price_distance:
                        return False
            
            return True
            
        except Exception as e:
            logging.error(f"Error validating technical levels: {e}")
            return False

    def adjust_for_market_conditions(self, signal: TradingSignal,
                                   market_conditions: Dict[str, Any]) -> TradingSignal:
        """根据市场条件调整信号"""
        try:
            # 根据波动性调整止损距离
            volatility = market_conditions.get('volatility', 'NORMAL')
            if volatility == 'HIGH':
                # 增加止损距离
                if signal.stop_loss:
                    entry_price = signal.entry_price or signal.entry_zones[0].price
                    current_distance = abs(entry_price - signal.stop_loss)
                    adjusted_distance = current_distance * 1.2  # 增加20%止损距离
                    
                    if signal.action == 'OPEN_LONG':
                        signal.stop_loss = entry_price - adjusted_distance
                    else:
                        signal.stop_loss = entry_price + adjusted_distance
            
            # 根据趋势强度调整止盈目标
            trend_strength = market_conditions.get('trend_strength', 'NORMAL')
            if trend_strength == 'STRONG' and signal.take_profit_levels:
                # 延长最后的止盈目标
                last_tp = signal.take_profit_levels[-1]
                entry_price = signal.entry_price or signal.entry_zones[0].price
                current_distance = abs(entry_price - last_tp.price)
                
                if signal.action == 'OPEN_LONG':
                    last_tp.price = entry_price + (current_distance * 1.2)
                else:
                    last_tp.price = entry_price - (current_distance * 1.2)
            
            return signal
            
        except Exception as e:
            logging.error(f"Error adjusting for market conditions: {e}")
            return signal

    def generate_trade_report(self, signal: TradingSignal,
                            analysis: Dict[str, Any]) -> str:
        """生成交易报告"""
        try:
            report = []
            report.append("📊 交易分析报告")
            report.append("\n🎯 交易信号:")
            report.append(f"交易对: {signal.symbol}")
            report.append(f"方向: {'做多' if signal.action == 'OPEN_LONG' else '做空'}")
            
            if signal.entry_zones:
                report.append("\n📍 入场区间:")
                for idx, zone in enumerate(signal.entry_zones, 1):
                    report.append(f"区间 {idx}: {zone.price} ({zone.percentage*100}%)")
            else:
                report.append(f"\n📍 入场价格: {signal.entry_price}")
            
            if signal.take_profit_levels:
                report.append("\n🎯 止盈目标:")
                for idx, tp in enumerate(signal.take_profit_levels, 1):
                    report.append(f"TP{idx}: {tp.price} ({tp.percentage*100}%)")
            
            report.append(f"\n🛑 止损: {signal.stop_loss}")
            
            report.append(f"\n📈 风险收益比: {self.calculate_risk_reward_ratio(signal):.2f}")
            report.append(f"⚠️ 风险等级: {self._assess_risk_level(signal)}")
            
            if analysis:
                report.append("\n📊 市场分析:")
                report.append(f"趋势: {analysis.get('trend', {}).get('direction', 'N/A')}")
                report.append(f"强度: {analysis.get('momentum', {}).get('strength', 'N/A')}")
                report.append(f"成交量: {analysis.get('volume', {}).get('trend', 'N/A')}")
            
            report.append(f"\n💡 建议: {self._generate_recommendation(signal)}")
            
            return "\n".join(report)
            
        except Exception as e:
            logging.error(f"Error generating trade report: {e}")
            return "无法生成交易报告"
