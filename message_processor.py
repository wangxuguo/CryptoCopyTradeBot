# message_processor.py
import time
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import logging
import re
import json
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from models import (
    TradingSignal, 
    ChannelMessage, 
    EntryZone, 
    TakeProfitLevel
)
from typing import Dict, List



# 首先定义 SymbolFormatter 类
class SymbolFormatter:
    """工具类用于格式化交易对符号"""
    
    @staticmethod
    def normalize_from_exchange(symbol: str, exchange: str) -> str:
        """从交易所格式转换为标准格式"""
        try:
            # 移除特殊后缀
            symbol = symbol.split(':')[0]
            
            # 处理不同交易所的格式
            if exchange == 'BINANCE':
                # 将 BTCUSDT 转换为 BTC/USDT
                if 'USDT' in symbol:
                    base = symbol.replace('USDT', '')
                    return f"{base}/USDT"
                return symbol
                
            elif exchange == 'OKX':
                # 将 BTC-USDT-SWAP 转换为 BTC/USDT
                if '-SWAP' in symbol:
                    symbol = symbol.replace('-SWAP', '')
                return symbol.replace('-', '/')
                
            return symbol
            
        except Exception as e:
            logging.error(f"Error normalizing symbol: {e}")
            return symbol

    @staticmethod
    def to_exchange_format(symbol: str, exchange: str) -> str:
        """转换为交易所特定格式"""
        try:
            # 清理符号
            symbol = symbol.upper().strip()
            symbol = symbol.split(':')[0]
            
            if exchange == 'BINANCE':
                # 转换为 BTCUSDT 格式
                if '/' in symbol:
                    base = symbol.split('/')[0]
                    return f"{base}USDT"
                elif not symbol.endswith('USDT'):
                    return f"{symbol}USDT"
                return symbol
                
            elif exchange == 'OKX':
                # 转换为 BTC-USDT-SWAP 格式
                if symbol.endswith('USDT'):
                    base = symbol[:-4]
                else:
                    base = symbol.replace('/', '')
                return f"{base}-USDT-SWAP"
                
            return symbol
            
        except Exception as e:
            logging.error(f"Error formatting symbol: {e}")
            return symbol


class MessageProcessor:
    def __init__(self, trading_logic, db, config):
        self.trading_logic = trading_logic
        self.db = db
        self.config = config

    def preprocess_message(self, message: str) -> str:
        """
        预处理和规范化交易信号消息
        处理交易对格式和其他清理工作
        """
        try:
            # 基本清理
            cleaned = re.sub(r'[^\w\s.,#@$%+-=:()]', ' ', message)
            cleaned = cleaned.replace(',', '')
            
            # 处理交易对格式
            def normalize_symbol(match):
                symbol = match.group(1)
                return f"#{SymbolFormatter.normalize_from_exchange(symbol, 'BINANCE')}"
                
            # 匹配并转换交易对格式
            # 处理 #BTC、#BTCUSDT 等格式
            cleaned = re.sub(r'#(\w+(?:usdt)?)', normalize_symbol, cleaned, flags=re.IGNORECASE)
            # 处理 $BTC、$BTCUSDT 等格式
            cleaned = re.sub(r'\$(\w+(?:usdt)?)', normalize_symbol, cleaned, flags=re.IGNORECASE)
            
            return cleaned.strip()
            
        except Exception as e:
            logging.error(f"Error preprocessing message: {e}")
            return message

    async def validate_signal(self, signal: TradingSignal, exchange_client) -> Tuple[bool, str]:
        """验证交易信号的有效性"""
        try:
            if not signal.is_valid():
                return False, "信号基本验证失败"

            # 获取市场信息
            market_info = await exchange_client.get_market_info(signal.symbol)
            if not market_info:
                return False, f"无法获取{signal.symbol}的市场信息"

            current_price = market_info.last_price
            
            # 验证价格合理性
            if signal.entry_price:
                price_deviation = abs(signal.entry_price - current_price) / current_price
                if price_deviation > 0.1:  # 价格偏离超过10%
                    return False, "入场价格偏离当前市场价格过大"

            # 验证风险收益比
            risk_ratio = signal.calculate_risk_ratio()
            if risk_ratio < 1.5:
                return False, "风险收益比不足1.5"

            return True, "验证通过"

        except Exception as e:
            logging.error(f"Error validating signal: {e}")
            return False, f"验证过程发生错误: {str(e)}"

    def _parse_type1_signal(self, text: str) -> Optional[TradingSignal]:
        """解析第一种类型的信号
        例如：
        #ARKM/USDT #SHORT
        BUY : 1,6750$-1,7100$
        TARGET 1 : 1,6600$ TARGET 2 : 1,6490$ TARGET 3 : 1,6260$
        STOP LOSS : 1,7650$
        """
        try:
            lines = text.split('\n')
            signal_data = {}
            
            # 解析第一行获取交易对和方向
            first_line = lines[0].upper()
            symbols = re.findall(r'#(\w+/USDT|\w+USDT)', first_line)
            if not symbols:
                return None
                
            signal_data['symbol'] = symbols[0].replace('/', '')
            signal_data['action'] = 'OPEN_SHORT' if 'SHORT' in first_line else 'OPEN_LONG'
            
            # 解析入场价格范围
            entry_line = next((l for l in lines if 'BUY' in l.upper() or 'ENTRY' in l.upper()), None)
            if entry_line:
                prices = re.findall(r'[\d.]+', entry_line)
                if len(prices) >= 2:  # 区间入场
                    left_price = float(prices[0])
                    right_price = float(prices[1])
                    mid_price = (left_price + right_price) / 2
                    
                    signal_data['entry_zones'] = [
                        EntryZone(left_price, 0.3),
                        EntryZone(mid_price, 0.5),
                        EntryZone(right_price, 0.2)
                    ]
                elif len(prices) == 1:  # 单一入场价格
                    signal_data['entry_price'] = float(prices[0])
            
            # 解析止盈目标
            tp_levels = []
            for line in lines:
                if 'TARGET' in line.upper():
                    match = re.search(r'[\d.]+', line)
                    if match:
                        price = float(match.group())
                        if len(tp_levels) == 0:
                            percentage = 0.4
                        elif len(tp_levels) == 1:
                            percentage = 0.3
                        else:
                            percentage = 0.3
                        tp_levels.append(TakeProfitLevel(price, percentage))
            
            if tp_levels:
                signal_data['take_profit_levels'] = tp_levels
            
            # 解析止损
            sl_line = next((l for l in lines if 'STOP LOSS' in l.upper()), None)
            if sl_line:
                match = re.search(r'[\d.]+', sl_line)
                if match:
                    signal_data['stop_loss'] = float(match.group())
            
            # 设置默认值
            signal_data['exchange'] = 'BINANCE'  # 默认使用Binance
            signal_data['position_size'] = self.config.DEFAULT_POSITION_SIZE
            signal_data['leverage'] = self.config.DEFAULT_LEVERAGE
            signal_data['margin_mode'] = 'cross'
            signal_data['dynamic_sl'] = self.config.ENABLE_DYNAMIC_SL
            
            signal = TradingSignal(**signal_data)
            return signal if signal.is_valid() else None
            
        except Exception as e:
            logging.error(f"Error parsing type 1 signal: {e}")
            return None

    def _parse_type2_signal(self, text: str) -> Optional[TradingSignal]:
        """解析第二种类型的信号
        例如：
        #CTK short, 0.652 entry
        #ENA long, 0.379 entry
        """
        try:
            # 解析交易对
            symbol_match = re.search(r'#(\w+)', text)
            if not symbol_match:
                return None
            
            symbol = symbol_match.group(1).upper() + 'USDT'
            
            # 解析方向
            direction = 'OPEN_LONG' if 'long' in text.lower() else 'OPEN_SHORT'
            
            # 解析入场价格
            price_match = re.search(r'([\d.]+)\s*entry', text)
            if not price_match:
                return None
            
            entry_price = float(price_match.group(1))
            
            # 计算默认止盈价格 (70% 移动)
            if direction == 'OPEN_LONG':
                take_profit = entry_price * 1.7
            else:
                take_profit = entry_price * 0.3
            
            signal_data = {
                'exchange': 'BINANCE',
                'symbol': symbol,
                'action': direction,
                'entry_price': entry_price,
                'take_profit_levels': [TakeProfitLevel(take_profit, 1.0)],
                'stop_loss': None,  # 第二种类型没有止损
                'position_size': self.config.DEFAULT_POSITION_SIZE,
                'leverage': self.config.DEFAULT_LEVERAGE,
                'margin_mode': 'cross'
            }
            
            signal = TradingSignal(**signal_data)
            return signal if signal.is_valid() else None
            
        except Exception as e:
            logging.error(f"Error parsing type 2 signal: {e}")
            return None
    async def resend_message_text_to_user(self, bot, target_user_id: int, text: str):
        await bot.send_message(chat_id=target_user_id, text=text)
    async def resend_message_to_user(self, update=None, context=None, target_user_id: int = 0, prefer_copy: bool = True, bot=None, message=None):
        try:
            if bot and message and getattr(message, 'id', None):
                from_chat_id = None
                if getattr(message, 'chat', None):
                    from_chat_id = getattr(message.chat, 'id', None)
                if not from_chat_id:
                    from_chat_id = getattr(message, 'chat_id', None)
                if from_chat_id:
                    try:
                        await bot.forward_message(
                            chat_id=target_user_id,
                            from_chat_id=from_chat_id,
                            message_id=message.id,
                        )
                        return
                    except Exception as e:
                        logging.warning(f"Forward message failed: {e}")
                    if prefer_copy:
                        try:
                            await bot.copy_message(
                                chat_id=target_user_id,
                                from_chat_id=from_chat_id,
                                message_id=message.id,
                            )
                            return
                        except Exception as e:
                            logging.warning(f"Copy message failed: {e}")
            if getattr(update, 'message', None) and update.message.message_id and context:
                try:
                    await context.bot.forward_message(
                        chat_id=target_user_id,
                        from_chat_id=update.message.chat_id,
                        message_id=update.message.message_id,
                    )
                    return
                except Exception as e:
                    logging.warning(f"Forward message failed: {e}")
                if prefer_copy:
                    try:
                        await context.bot.copy_message(
                            chat_id=target_user_id,
                            from_chat_id=update.message.chat_id,
                            message_id=update.message.message_id,
                        )
                        return
                    except Exception as e:
                        logging.warning(f"Copy message failed: {e}")
            text = (
                getattr(message, 'text', None)
                or getattr(message, 'caption', None)
                or getattr(update.message, 'text', None)
                or getattr(update.message, 'caption', None)
                or ''
            )
            if text:
                if bot:
                    await bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                    )
                elif context:
                    await context.bot.send_message(
                        chat_id=target_user_id,
                        text=text,
                    )
            else:
                logging.error("No text or caption to resend")
        except Exception as e:
            logging.error(f"Error resending message: {e}")
    # message_processor.py 中的 MessageProcessor 类
    async def process_channel_message(self, event, client, bot) -> Optional[List[TradingSignal]]:
        """处理频道消息（支持多个交易信号）"""
        try:
            # 验证事件对象
            if not event:
                logging.error("Invalid event object")
                return None

            # 获取消息对象
            message = getattr(event, 'message', None) or event.channel_post  # 添加对channel_post的支持
            if not message or not message.text:
                logging.error("Invalid message or empty text")
                return None

            # 获取并验证chat对象
            try:
                chat = None
                if hasattr(event, 'chat'):
                    chat = event.chat
                elif hasattr(message, 'chat'):
                    chat = message.chat
                elif hasattr(event, 'channel_post'):
                    chat = event.channel_post.chat

                if not chat:
                    logging.error("Could not get chat info")
                    return None

                channel_id = chat.id  # 使用chat对象的id
            except Exception as e:
                logging.error(f"Error getting chat: {e}")
                return None

            # 安全获取时间戳
            timestamp = getattr(message, 'date', None)
            if timestamp:
                if isinstance(timestamp, datetime):
                    timestamp = int(timestamp.timestamp())
                else:
                    timestamp = int(timestamp)
            else:
                timestamp = int(time.time())

            # 创建消息对象
            channel_message = ChannelMessage(
                channel_id=channel_id,
                message_id=message.id,
                text=message.text,
                timestamp=datetime.fromtimestamp(timestamp),
                channel_title=getattr(chat, 'title', str(channel_id)),
                channel_username=getattr(chat, 'username', None)
            )

            # 检查频道是否被监控
            channel_info = self.db.get_channel_info(channel_message.channel_id)
            logging.info(f"ChannelInfo -- from message db--{channel_info}")
            if not channel_info or not channel_info['is_active'] or channel_info['channel_type']!='MONITOR':
                return None
            # 完全转发原始消息（包括媒体、表情等所有内容）
            try:
                target_group_id = self.db._normalize_channel_id(-4813705648)
                source_channel_id = self.db._normalize_channel_id(channel_id)
                await bot.forward_message(
                    chat_id=target_group_id,
                    from_chat_id=source_channel_id,
                    message_id=message.id
                )
            except Exception as e:
                logging.warning(f"转发消息到群组失败: {e} | chat_id={target_group_id} from_chat_id={source_channel_id} message_id={message.id}")
                try:
                    await self.resend_message_to_user(
                        bot=bot,
                        target_user_id=target_group_id,
                        message=message
                    )
                except Exception as e:
                    logging.error(f"复制消息到群组失败: {e}")
                    try:
                      
                        await bot.copy_message(
                            chat_id=target_group_id,
                            from_chat_id=source_channel_id,
                            message_id=message.id
                        )
                    except Exception as e:
                        logging.error(f"复制消息到群组失败: {e}")
                        try:
                            fallback_text = getattr(message, 'text', None) or getattr(message, 'caption', None)
                            if fallback_text:
                                await self.resend_message_text_to_user(
                                    bot=bot,
                                    target_user_id=target_group_id,
                                    text=fallback_text
                                )
                            else:
                                logging.error("无法提取任何可转发的内容")
                        except Exception as e:
                            logging.error(f"转发消息到群组失败: {e}")
            cleaned_message = self.preprocess_message(channel_message.text)

            context_append = ""
            try:
                exm = getattr(self.trading_logic, 'exchange_manager', None)
                if exm:
                    positions_by_ex = await exm.get_positions()
                    orders_by_ex = await exm.get_open_orders()
                    if positions_by_ex:
                        lines: List[str] = []
                        for ex_name, positions in positions_by_ex.items():
                            lines.append(f"{ex_name}:")
                            for pos in positions:
                                raw_side = pos.get('side') if hasattr(pos, 'get') else getattr(pos, 'side', None)
                                if hasattr(raw_side, 'value'):
                                    raw_side = raw_side.value
                                side = '做多'
                                if isinstance(raw_side, str):
                                    normalized = raw_side.strip().upper()
                                    if normalized in ('SHORT', 'OPEN_SHORT', 'SELL'):
                                        side = '做空'
                                    elif normalized in ('LONG', 'OPEN_LONG', 'BUY'):
                                        side = '做多'
                                else:
                                    size_value = pos.get('size') if hasattr(pos, 'get') else getattr(pos, 'size', 0)
                                    if isinstance(size_value, (int, float)) and size_value < 0:
                                        side = '做空'
                                size_value = pos.get('size') if hasattr(pos, 'get') else getattr(pos, 'size', 0)
                                lines.append(
                                    f"{getattr(pos, 'symbol', '')} {side} 量: {abs(size_value)} 入场: {getattr(pos, 'entry_price', 0):.6f} 未盈亏: {getattr(pos, 'unrealized_pnl', 0):.2f}"
                                )
                        if lines:
                            context_append += "\n\n当前持仓:\n" + "\n".join(lines)
                    if orders_by_ex:
                        lines: List[str] = []
                        for ex_name, orders in orders_by_ex.items():
                            lines.append(f"{ex_name}:")
                            for od in orders:
                                lines.append(
                                    f"{getattr(od, 'symbol', '')} {getattr(od, 'side', '')} {getattr(od, 'type', '')} 量: {getattr(od, 'amount', 0)} 价: {getattr(od, 'price', 0) if getattr(od, 'price', None) is not None else ''} 状态: {getattr(od, 'status', '')}"
                                )
                        if lines:
                            context_append += "\n\n当前委托:\n" + "\n".join(lines)
            except Exception as e:
                logging.error(f"Error building context: {e}")

            if context_append:
                cleaned_message = cleaned_message + context_append
            
            # 使用自定义prompt或默认prompt
            custom_prompt = channel_info.get('prompt')
                
            # 尝试解析信号
            trading_signals = await self.trading_logic.process_message(
                cleaned_message,
                custom_prompt
            )
        
            await self.resend_message_text_to_user(
                bot=bot,
                target_user_id=584536494,
                text=cleaned_message
            )
            await self.resend_message_text_to_user(
                bot=bot,
                target_user_id=8184692730,
                text=cleaned_message
            )
            if trading_signals:
                forward_channels = None
                if bot:
                    forward_channels = self.db.get_forward_channels(channel_message.channel_id)
                    logging.info(f"ForwardChannels---{forward_channels}--senderchannel---{channel_message.channel_id}")
                processed: List[TradingSignal] = []
                for trading_signal in trading_signals:
                    # 验证交易对是否存在
                    if not await self._validate_trading_pair(trading_signal):
                        if bot:  # 确保bot存在
                            await self._notify_invalid_pair(
                                bot, 
                                channel_info.get('forward_channel_id'),
                                trading_signal.symbol
                            )
                        continue
                    # 添加源信息
                    trading_signal.source_message = channel_message.text
                    trading_signal.source_channel = channel_message.channel_id
                    # 保存信号到数据库
                    signal_id = self.db.add_signal_tracking(trading_signal)
                    if signal_id > 0:
                        trading_signal.signal_id = signal_id
                        processed.append(trading_signal)
                        # 转发到目标频道
                        if bot and forward_channels:
                            for forward_channel in forward_channels:
                                if forward_channel and 'channel_id' in forward_channel:
                                    f_channel_id = forward_channel['channel_id']
                                    if (f_channel_id > 0):
                                        f_channel_id = -int(f"100{f_channel_id}")
                                    await self.forward_signal(
                                        trading_signal,
                                        f_channel_id,
                                        bot
                                    )
                    time.sleep(10)
                return processed if processed else None

            return None
                    
        except Exception as e:
            logging.error(f"Error processing channel message: {e}")
            import traceback
            logging.error(f"Traceback: {traceback.format_exc()}")
            return None

    async def _validate_trading_pair(self, signal: TradingSignal) -> bool:
        """验证交易对是否存在"""
        # TODO: 实现实际的验证逻辑
        return True

    async def _notify_invalid_pair(self, client, channel_id: int, symbol: str):
        """通知无效的交易对"""
        message = (
            f"⚠️ 警告: 交易对 {symbol} 在交易所中不存在\n"
            f"请检查交易对名称是否正确。"
        )
        try:
            await client.send_message(channel_id, message)
        except Exception as e:
            logging.error(f"Error sending invalid pair notification: {e}")

    # 在 message_processor.py 中修改
    async def forward_signal(self, signal: TradingSignal, forward_channel_id: int, bot) -> bool:
        """转发处理后的交易信号"""
        try:
            # 确保使用完整的频道ID格式
            full_channel_id = self.db._normalize_channel_id(forward_channel_id)
            
            # 尝试发送消息
            message = self._format_signal_message(signal)
            keyboard = [
                [
                    InlineKeyboardButton("✅ 执行交易", callback_data=f"execute_{signal.symbol}_{signal.signal_id}"),
                    InlineKeyboardButton("❌ 忽略", callback_data=f"ignore_{signal.signal_id}")
                ],
                [
                    InlineKeyboardButton("📊 查看分析", callback_data=f"analysis_{signal.symbol}_{signal.signal_id}")
                ]
            ]

            try:
                await bot.send_message(
                    chat_id=full_channel_id,
                    text=message,
                    parse_mode='HTML',
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                return True
            except Exception as e:
                logging.error(f"Error sending message to channel {forward_channel_id}: {e}")
                if "Chat not found" in str(e):
                    self.db.update_channel_status(forward_channel_id, False)
                return False
                
        except Exception as e:
            logging.error(f"Error forwarding signal: {e}")
            return False
    def _format_signal_message(self, signal: TradingSignal) -> str:
        """格式化信号消息"""
        try:
            action_emoji = {
                'OPEN_LONG': '🟢 做多',
                'OPEN_SHORT': '🔴 做空',
                'CLOSE': '⚪️ 平仓'
            }
            
            message = (
                f"<b>💹 交易信号</b>\n\n"
                f"交易所: {signal.exchange}\n"
                f"交易对: {signal.symbol}\n"
                f"方向: {action_emoji.get(signal.action, signal.action)}\n"
                f"杠杆: {signal.leverage}X\n"
                f"仓位: ${signal.position_size}\n\n"
            )
            
            if signal.entry_zones:
                message += "📍 入场区间:\n"
                for idx, zone in enumerate(signal.entry_zones, 1):
                    message += (
                        f"Zone {idx}: ${zone.price:.4f} "
                        f"({zone.percentage * 100:.1f}%)\n"
                    )
            elif signal.entry_price:
                message += f"📍 入场价格: ${signal.entry_price:.4f}\n"
                
            if signal.take_profit_levels:
                message += "\n🎯 止盈目标:\n"
                for idx, tp in enumerate(signal.take_profit_levels, 1):
                    message += (
                        f"TP{idx}: ${tp.price:.4f} "
                        f"({tp.percentage * 100:.1f}%)\n"
                    )
                    
            if signal.stop_loss:
                message += f"\n🛑 止损: ${signal.stop_loss:.4f}"
                
            if signal.dynamic_sl:
                message += "\n⚡️ 动态止损已启用"
            
            # 添加风险等级
            risk_emoji = {'LOW': '🟢', 'MEDIUM': '🟡', 'HIGH': '🔴'}
            message += f"\n\n⚠️ 风险等级: {risk_emoji.get(signal.risk_level, '⚪️')} {signal.risk_level}"
            
            # 添加置信度
            confidence = int(signal.confidence * 100) if signal.confidence else 0
            message += f"\n📊 置信度: {confidence}%"
            
            return message

        except Exception as e:
            logging.error(f"Error formatting signal message: {e}")
            return "Error formatting message"

    async def handle_callback_query(self, callback_query, client):
        """处理回调查询"""
        try:
            data = callback_query.data
            user_id = callback_query.from_user.id
            
            # 验证用户权限
            if user_id != self.config.OWNER_ID:
                await callback_query.answer("未授权的操作")
                return
            
            if data.startswith('execute_'):
                _, symbol, signal_id = data.split('_')
                await self._handle_execute_signal(callback_query, symbol, int(signal_id))
            elif data.startswith('ignore_'):
                _, signal_id = data.split('_')
                await self._handle_ignore_signal(callback_query, int(signal_id))
            elif data.startswith('analysis_'):
                _, symbol, signal_id = data.split('_')
                await self._handle_show_analysis(callback_query, symbol, int(signal_id))
                
        except Exception as e:
            logging.error(f"Error handling callback query: {e}")
            await callback_query.answer("处理请求时发生错误")

    async def _handle_execute_signal(self, callback_query, symbol: str, signal_id: int):
        """处理执行信号的回调"""
        try:
            # 获取信号信息
            signal_info = self.db.get_signal_info(signal_id)
            if not signal_info:
                await callback_query.answer("信号不存在或已过期")
                return
            
            # 更新状态为执行中
            self.db.update_signal_status(signal_id, 'EXECUTING')
            
            # 通知用户
            await callback_query.answer("开始执行交易指令")
            
            # 修改消息显示执行状态
            original_message = callback_query.message.text
            await callback_query.message.edit_text(
                original_message + "\n\n⚙️ 正在执行交易...",
                parse_mode='HTML'
            )
            
            # TODO: 这里需要集成实际的交易执行逻辑
            # signal = self.trading_logic.execute_signal(signal_info)
            
            # 临时模拟成功
            await callback_query.message.edit_text(
                original_message + "\n\n✅ 交易已执行",
                parse_mode='HTML'
            )
            
        except Exception as e:
            logging.error(f"Error executing signal: {e}")
            await callback_query.answer("执行交易时发生错误")

    async def _handle_ignore_signal(self, callback_query, signal_id: int):
        """处理忽略信号的回调"""
        try:
            # 更新信号状态
            self.db.update_signal_status(signal_id, 'IGNORED')
            
            # 更新消息
            original_message = callback_query.message.text
            await callback_query.message.edit_text(
                original_message + "\n\n❌ 已忽略此信号",
                parse_mode='HTML'
            )
            
            await callback_query.answer("已忽略此交易信号")
            
        except Exception as e:
            logging.error(f"Error ignoring signal: {e}")
            await callback_query.answer("操作失败")

    async def _handle_show_analysis(self, callback_query, symbol: str, signal_id: int):
        """处理显示分析的回调"""
        try:
            # 获取信号信息
            signal_info = self.db.get_signal_info(signal_id)
            if not signal_info:
                await callback_query.answer("信号不存在或已过期")
                return
            
            # 生成分析报告
            analysis = await self.trading_logic.generate_analysis(signal_info)
            
            # 发送分析结果
            analysis_message = (
                "📊 交易分析报告\n\n"
                f"交易对: {symbol}\n"
                f"当前价格: {analysis.get('current_price', 'N/A')}\n"
                f"市场趋势: {analysis.get('trend', 'N/A')}\n\n"
                "技术指标:\n"
                f"RSI: {analysis.get('rsi', 'N/A')}\n"
                f"MACD: {analysis.get('macd', 'N/A')}\n"
                f"成交量: {analysis.get('volume', 'N/A')}\n\n"
                f"建议: {analysis.get('recommendation', 'N/A')}\n"
                f"风险等级: {analysis.get('risk_level', 'N/A')}"
            )
            
            await callback_query.message.reply_text(
                analysis_message,
                parse_mode='HTML'
            )
            
        except Exception as e:
            logging.error(f"Error showing analysis: {e}")
            await callback_query.answer("无法生成分析报告")

    def extract_signal_info(self, message_text: str) -> Dict[str, Any]:
        """从消息文本中提取信号信息"""
        try:
            lines = message_text.split('\n')
            signal_info = {}
            
            for line in lines:
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip().lower().replace(' ', '_')
                    value = value.strip()
                    
                    # 处理特殊字段
                    if key == 'action':
                        value = value.replace('🟢', '').replace('🔴', '').replace('⚪️', '').strip()
                    elif key in ['entry_price', 'take_profit', 'stop_loss', 'position_size']:
                        value = float(value.replace('$', '').replace(',', ''))
                        
                    signal_info[key] = value
            
            return signal_info
            
        except Exception as e:
            logging.error(f"Error extracting signal info: {e}")
            return {}

    async def notify_error(self, client, channel_id: int, error_message: str):
        """发送错误通知"""
        try:
            message = (
                "❌ 错误通知\n\n"
                f"{error_message}"
            )
            await client.send_message(channel_id, message)
        except Exception as e:
            logging.error(f"Error sending notification: {e}")
            
    def get_signal_info(self, signal_id: int) -> Optional[Dict[str, Any]]:
        """从数据库获取信号信息"""
        return self.db.get_signal_info(signal_id)

    async def process_error(self, error: Exception, update, context):
        """处理错误"""
        logging.error(f"Update {update} caused error {error}")
        try:
            if self.config.OWNER_ID:
                await context.bot.send_message(
                    chat_id=self.config.OWNER_ID,
                    text=f"❌ 发生错误:\n{str(error)}"
                )
        except Exception as e:
            logging.error(f"Error sending error notification: {e}")
