# main.py
import asyncio
from datetime import datetime, timedelta
import json
import logging
import sys
from typing import Any, Dict, List, Optional
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters
)
from telegram import (
    BotCommand,
    CallbackQuery,
    Message,
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler
)
from telethon import TelegramClient, events

from channel_management import ChannelManagement
from config import Config
from exchange_execution import ExchangeManager, OrderParams, PositionSide
from message_processor import MessageProcessor
from database import Database
from main_menu import MainMenuManager
from settings import SettingsManager, StatisticsManager
from models import EntryZone, TakeProfitLevel, OrderResult
from trading_logic import TradingLogic, TradingSignal
from button_texts import ButtonText as BT  # 导入按钮文本配置

class TradingBot:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database(config.DATABASE_NAME)
        self.exchange_manager = ExchangeManager(config)
        self.trading_logic = TradingLogic(
            config.DEEPSEEK_API_KEY,
            config.OPENAI_API_KEY,
            config.OPENAI_API_BASE_URL,
            self.exchange_manager
        )
        self.exchanges = self.exchange_manager.exchanges
        self.message_processor = MessageProcessor(
            self.trading_logic,
            self.db,
            self.config
        )
        self.main_menu = MainMenuManager(self)
        
    
        self.settings_manager = SettingsManager(self)
        self.stats_manager = StatisticsManager(self)
        self.exchange_manager.set_success_callback(self._notify_execute_success)
        
        # Initialize Telegram bot
        self.application = Application.builder().token(config.TELEGRAM_TOKEN).build()
        
        # Initialize Telethon client
        self.client = TelegramClient(
            config.SESSION_NAME,
            config.API_ID,
            config.API_HASH
        )
        
        # Initialize UI components
        self.channel_management = ChannelManagement(self.db, self.config,self.client)
        
        # Setup handlers
        self.setup_handlers()
        
        # Setup commands
        asyncio.create_task(self.setup_commands())



    async def handle_channel_message(self, event):
        """处理频道消息"""
        try:
            # 获取消息对象
            message = getattr(event, 'message', None) or event.channel_post
            if not message or not message.text:
                logging.info(f"Invalid message text-IGNORE")
                return

            # 获取频道信息
            chat = message.chat
            if not chat:
                logging.error("Could not get chat info")
                return

            channel_id = chat.id

            # 检查频道是否被监控
            channel_info = self.db.get_channel_info(channel_id)
            if not channel_info or not channel_info['is_active']:
                return

            network_indicator = "🏮 测试网" if self.config.trading.use_testnet else "🔵 主网"

            # 处理消息
            signals = await self.message_processor.process_channel_message(
                event=event,
                client=self.client,
                bot=self.application.bot
            )

            if signals:
                for signal in signals:
                    if signal.is_valid():
                        if self.config.trading.auto_trade_enabled:
                            result = await self.exchange_manager.execute_signal(signal)
                            if not result.success:
                                await self.notify_owner(
                                    f"{network_indicator} 自动交易执行失败\n\n"
                                    f"交易对: {signal.symbol}\n"
                                    f"错误: {result.error_message}"
                                )
                        else:
                            message_text = (
                                f"{network_indicator} 新交易信号\n\n"
                                f"来源: {chat.title}\n"
                                f"交易对: {signal.symbol}\n"
                                f"方向: {'做多' if signal.action == 'OPEN_LONG' else '做空'}\n"
                            )
                            if signal.entry_zones:
                                message_text += "\n入场区间:\n"
                                for zone in signal.entry_zones:
                                    message_text += f"- ${zone.price} ({zone.percentage * 100}%)\n"
                            else:
                                message_text += f"\n入场价格: ${signal.entry_price}"
                            if signal.take_profit_levels:
                                message_text += "\n\n止盈目标:\n"
                                for tp in signal.take_profit_levels:
                                    message_text += f"- ${tp.price} ({tp.percentage * 100}%)\n"
                            if signal.stop_loss:
                                message_text += f"\n止损: ${signal.stop_loss}"
                            keyboard = [
                                [
                                    InlineKeyboardButton(
                                        "✅ 执行交易",
                                        callback_data=f"execute_{signal.symbol}_{signal.signal_id}"
                                    ),
                                    InlineKeyboardButton(
                                        "❌ 忽略",
                                        callback_data=f"ignore_{signal.signal_id}"
                                    )
                                ],
                                [
                                    InlineKeyboardButton(
                                        "📊 查看分析",
                                        callback_data=f"analysis_{signal.symbol}_{signal.signal_id}"
                                    )
                                ]
                            ]
                            try:
                                await self.application.bot.send_message(
                                    chat_id=self.config.OWNER_ID,
                                    text=message_text,
                                    reply_markup=InlineKeyboardMarkup(keyboard)
                                )
                            except Exception as e:
                                logging.error(f"Error sending notification: {e}")
                                await self.notify_owner(f"发送通知失败: {str(e)}")

        except Exception as e:
            error_msg = f"Error handling channel message: {e}"
            logging.error(error_msg)
            import traceback
            logging.error(f"Full traceback:\n{traceback.format_exc()}")
            try:
                await self.notify_owner(f"❌ {error_msg}")
            except:
                pass

    def setup_handlers(self):
        """设置所有消息处理器"""
        # 命令处理器
        commands = [
            CommandHandler("start", self.start_command),
            CommandHandler("help", self.help_command),
            CommandHandler("stats", self.stats_command),
            CommandHandler("balance", self.balance_command),
            CommandHandler("positions", self.positions_command),
            CommandHandler("channels", self._handle_channels_command),  # 使用新的处理方法,
            CommandHandler("settings", lambda update, context: 
                         self.show_settings(update.message))
        ]
        
        for handler in commands:
            self.application.add_handler(handler)

        # 添加频道管理处理器
        for handler in self.channel_management.get_handlers():
            self.application.add_handler(handler)

        # 回调查询处理器
        self.application.add_handler(CallbackQueryHandler(self.handle_callback_query))
        
        # 错误处理器
        self.application.add_error_handler(self.error_handler)
        
        # 添加主菜单处理器
        self.application.add_handler(CommandHandler("start", self.main_menu.setup_main_menu))
        # self.application.add_handler(MessageHandler(
        #     filters.TEXT & ~filters.COMMAND,
        #     self.main_menu.handle_menu_selection
        # ))

    async def show_main_menu(self, message):
        """显示主菜单"""
        # 添加测试网标识
        network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
        
        keyboard = [
            [
                InlineKeyboardButton(BT.CHANNEL_MANAGEMENT, callback_data="channel_management"),
                InlineKeyboardButton(BT.TRADE_MANAGEMENT, callback_data="trade_management")
            ],
            [
                InlineKeyboardButton(BT.POSITION_OVERVIEW, callback_data="positions"),
                InlineKeyboardButton(BT.ACCOUNT_STATS, callback_data="account_stats")
            ],
            [
                InlineKeyboardButton(BT.SETTINGS, callback_data="settings"),
                InlineKeyboardButton(BT.HELP, callback_data="help")
            ]
        ]
        
        await message.edit_text(
            f"{network_indicator} 交易机器人\n\n"
            "请从下面的菜单中选择一个选项:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def show_trade_management(self, message):
        """显示交易管理菜单"""
        keyboard = [
            [
                InlineKeyboardButton(BT.VIEW_POSITIONS, callback_data="view_positions"),
                InlineKeyboardButton(BT.CLOSE_POSITION, callback_data="close_position")
            ],
            [
                InlineKeyboardButton(BT.MODIFY_TP_SL, callback_data="modify_tp_sl"),
                InlineKeyboardButton(BT.ORDER_HISTORY, callback_data="order_history")
            ],
            [
                InlineKeyboardButton(BT.RISK_SETTINGS, callback_data="risk_settings"),
                InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")
            ]
        ]
        
        network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
        
        await message.edit_text(
            f"{network_indicator} 交易管理\n\n"
            "• 查看和管理持仓\n"
            "• 修改订单和设置\n"
            "• 查看交易历史",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def show_settings(self, update: Update):
        """显示设置菜单"""
        keyboard = [
            [
                InlineKeyboardButton(BT.RISK_SETTINGS, callback_data="risk_settings"),
                InlineKeyboardButton(BT.AUTO_TRADE_SETTINGS, callback_data="auto_trade_settings")
            ],
            [
                InlineKeyboardButton(BT.NOTIFICATION_SETTINGS, callback_data="notification_settings"),
                InlineKeyboardButton(BT.API_SETTINGS, callback_data="api_settings")
            ],
            [InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")]
        ]

        network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
        
        if isinstance(update, Message):
            await update.reply_text(
                f"{network_indicator} 机器人设置\n\n"
                "• 配置风险参数\n"
                "• 设置自动交易规则\n"
                "• 管理通知设置\n"
                "• 更新API配置",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.edit_text(
                f"{network_indicator} 机器人设置\n\n"
                "• 配置风险参数\n"
                "• 设置自动交易规则\n"
                "• 管理通知设置\n"
                "• 更新API配置",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
            
    async def show_positions_menu(self, message):
        """显示持仓概览和管理选项"""
        try:
            positions_by_exchange = await self.exchange_manager.get_positions()
            
            keyboard = []
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            position_text = f"{network_indicator} 当前持仓:\n\n"
            
            for exchange_name, exchange_positions in positions_by_exchange.items():
                active_positions = [p for p in exchange_positions if getattr(p, 'size', 0) != 0]
                if not active_positions:
                    continue
                    
                position_text += f"📈 {exchange_name}:\n"
                for pos in active_positions:
                    direction = BT.DIRECTION_LONG if pos.side == PositionSide.LONG else BT.DIRECTION_SHORT
                    position_text += (
                        f"{pos.symbol}: {direction}\n"
                        f"数量: {abs(pos.size):.4f}\n"
                        f"入场价: {pos.entry_price:.6f}\n"
                        f"未实现盈亏: {pos.unrealized_pnl:.2f} USDT\n\n"
                    )
                    keyboard.append([
                        InlineKeyboardButton(
                            f"修改 {pos.symbol}",
                            callback_data=f"modify_{exchange_name}_{pos.symbol}"
                        ),
                        InlineKeyboardButton(
                            f"平仓 {pos.symbol}",
                            callback_data=f"close_{exchange_name}_{pos.symbol}"
                        )
                    ])
            
            if not keyboard:
                position_text += "暂无持仓"
            
            keyboard.append([InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")])
            
            await message.edit_text(
                position_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logging.error(f"Error showing positions menu: {e}")
            await message.edit_text(
                "获取持仓信息失败，请稍后重试。",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")
                ]])
            )

    async def handle_position_modification(self, query: CallbackQuery):
        """处理持仓修改"""
        try:
            _, exchange, symbol = query.data.split('_')
            keyboard = [
                [
                    InlineKeyboardButton(BT.MODIFY_TP, callback_data=f"modify_tp_{exchange}_{symbol}"),
                    InlineKeyboardButton(BT.MODIFY_SL, callback_data=f"modify_sl_{exchange}_{symbol}")
                ],
                [InlineKeyboardButton(BT.BACK_MAIN, callback_data="positions")]
            ]
            
            position = await self.exchanges[exchange.upper()].fetch_positions(symbol)
            if not position:
                await query.message.edit_text("未找到持仓信息")
                return
            
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR    
            text = (
                f"{network_indicator} 修改持仓\n\n"
                f"交易对: {symbol}\n"
                f"当前价格: {position['entry_price']}\n"
                f"持仓大小: {position['size']}\n"
                "请选择要修改的选项:"
            )
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            
        except Exception as e:
            logging.error(f"Error in handle_position_modification: {e}")
            await query.message.edit_text("修改持仓时发生错误")

    async def handle_trade_execution(self, query: CallbackQuery):
        """处理交易执行"""
        try:
            # 解析回调数据
            _, symbol, signal_id = query.data.split('_')
            signal_info = self.db.get_signal_info(int(signal_id))
            
            if not signal_info:
                await query.answer("信号已过期或无效")
                return
            
            # 创建交易信号对象
            signal = TradingSignal(
                exchange=signal_info.get('exchange', 'BINANCE'),
                symbol=signal_info.get('symbol', symbol),
                action=signal_info.get('signal_type', 'OPEN_LONG'),  # 默认做多
                entry_price=signal_info.get('entry_price'),
                position_size=signal_info.get('position_size', self.config.trading.default_position_size),
                leverage=signal_info.get('leverage', self.config.trading.default_leverage),
                margin_mode=signal_info.get('margin_mode', 'cross')
            )

            # 处理止损和止盈
            if signal_info.get('stop_loss'):
                signal.stop_loss = float(signal_info['stop_loss'])
                
            if signal_info.get('take_profit_levels'):
                signal.take_profit_levels = [
                    TakeProfitLevel(tp['price'], tp['percentage'])
                    for tp in signal_info['take_profit_levels']
                ]
                
            if signal_info.get('entry_zones'):
                signal.entry_zones = [
                    EntryZone(zone['price'], zone['percentage'])
                    for zone in signal_info['entry_zones']
                ]

            # 执行交易
            if signal.is_valid():
                network_indicator = "🏮 测试网" if self.config.trading.use_testnet else "🔵 主网"
                
                # 更新UI显示执行状态
                await query.edit_message_text(
                    f"{network_indicator} 执行交易中...\n\n"
                    f"交易对: {signal.symbol}\n"
                    f"方向: {'做多' if signal.action == 'OPEN_LONG' else '做空'}\n"
                    f"杠杆: {signal.leverage}X",
                    reply_markup=None
                )

                # 执行交易
                result = await self.exchange_manager.execute_signal(signal)
                
                if result.success:
                    message = (
                        f"{network_indicator} 交易执行成功✅\n\n"
                        f"交易对: {signal.symbol}\n"
                        f"方向: {'做多' if signal.action == 'OPEN_LONG' else '做空'}\n"
                        f"订单ID: {result.order_id}\n"
                        f"执行价格: {result.executed_price}\n"
                        f"数量: {result.executed_amount}"
                    )
                    
                    # 如果有止损止盈单
                    if result.extra_info and 'orders' in result.extra_info:
                        message += "\n\n附加订单:"
                        for order in result.extra_info['orders']:
                            order_type = order.get('type', '')
                            if 'stop_loss' in order_type.lower():
                                message += f"\n止损订单: {order.get('order_id')}"
                            elif 'take_profit' in order_type.lower():
                                message += f"\n止盈订单: {order.get('order_id')}"
                else:
                    message = (
                        f"{network_indicator} 交易执行失败❌\n\n"
                        f"交易对: {signal.symbol}\n"
                        f"错误: {result.error_message}"
                    )
                
                # 更新消息
                await query.edit_message_text(
                    message,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("返回", callback_data="main_menu")
                    ]])
                )
            else:
                await query.answer("无效的交易信号")
                
        except Exception as e:
            logging.error(f"Error executing trade: {e}")
            import traceback
            logging.error(f"Traceback:\n{traceback.format_exc()}")
            await query.answer("执行交易时发生错误")
            
    async def handle_position_close(self, query: CallbackQuery):
        """处理持仓平仓"""
        try:
            _, exchange, symbol = query.data.split('_')
            
            keyboard = [
                [
                    InlineKeyboardButton(BT.CONFIRM_CLOSE, callback_data=f"confirm_close_{exchange}_{symbol}"),
                    InlineKeyboardButton(BT.CANCEL, callback_data="positions")
                ]
            ]
            
            position = await self.exchanges[exchange.upper()].fetch_positions(symbol)
            if not position:
                await query.message.edit_text("未找到持仓信息")
                return
                
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            direction = BT.DIRECTION_LONG if position['size'] > 0 else BT.DIRECTION_SHORT
            
            text = (
                f"{network_indicator} 确认平仓\n\n"
                f"交易对: {symbol}\n"
                f"持仓方向: {direction}\n"
                f"持仓大小: {abs(position['size'])}\n"
                f"开仓价格: {position['entry_price']}\n"
                f"未实现盈亏: {position['unrealized_pnl']:.2f} USDT\n\n"
                "确认要平仓此持仓吗?"
            )
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
            
        except Exception as e:
            logging.error(f"Error in handle_position_close: {e}")
            await query.message.edit_text("处理平仓请求时发生错误")

    async def handle_callback_query(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理所有回调查询"""
        query = update.callback_query
        data = query.data
        
        try:
            if data == "channel_management":
                await self.channel_management.show_channel_management(query.message)
            if data.startswith(("risk_", "auto_", "notification_", "api_")):
                await self.settings_manager.handle_settings_callback(update, context)
            elif data in ["detailed_stats", "export_stats"]:
                await self.stats_manager.handle_stats_callback(update, context)
            elif data == "trade_management":
                await self.show_trade_management(query.message)
            elif data == "positions":
                await self.show_positions_menu(query.message)
            elif data == "account_stats":
                await self.show_account_stats(query.message)
            elif data == "settings":
                await self.show_settings(query.message)
            elif data == "help":
                await self.show_help(query.message)
            elif data == "main_menu":
                await self.show_main_menu(query.message)
            elif data.startswith("execute_"):
                await self.handle_trade_execution(query)
            elif data.startswith("modify_"):
                await self.handle_position_modification(query)
            elif data.startswith("close_"):
                await self.handle_position_close(query)
            elif data.startswith("confirm_execute_"):
                await self.execute_confirmed_trade(query)
            elif data.startswith("confirm_close_"):
                await self.execute_confirmed_close(query)
            elif any(data.startswith(prefix) for prefix in ["add_", "remove_", "list_", "edit_", "view_", "manage_"]):
                await self.channel_management.handle_callback_query(update, context)
            else:
                await query.answer("未知的操作")
                
        except Exception as e:
            logging.error(f"Error in handle_callback_query: {e}")
            await query.answer("处理请求时发生错误")

    async def execute_confirmed_trade(self, query: CallbackQuery):
        """执行已确认的交易"""
        try:
            _, signal_id = query.data.split('_')
            signal_info = self.db.get_signal_info(int(signal_id))
            
            if not signal_info:
                await query.answer("信号已过期")
                return
            
            # 执行交易
            result = await self.exchange_manager.execute_signal(TradingSignal(**signal_info))
            
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            if result.success:
                message = (
                    f"{network_indicator} 交易执行成功\n\n"
                    f"交易对: {signal_info['symbol']}\n"
                    f"订单ID: {result.order_id}\n"
                    f"执行价格: {result.executed_price}\n"
                    f"执行数量: {result.executed_amount}"
                )
                await self.notify_trade_execution(signal_info, result)
            else:
                message = (
                    f"{network_indicator} 交易执行失败\n\n"
                    f"错误: {result.error_message}"
                )
                await self.notify_trade_error(signal_info, result.error_message)
            
            await query.message.edit_text(
                message,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")
                ]])
            )
            
        except Exception as e:
            logging.error(f"Error executing confirmed trade: {e}")
            await query.answer("执行交易时发生错误")

    async def execute_confirmed_close(self, query: CallbackQuery):
        """执行已确认的平仓"""
        try:
            _, exchange, symbol = query.data.split('_')
            
            result = await self.exchange_manager.close_position(exchange, symbol)
            
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            if result.success:
                message = (
                    f"{network_indicator} 平仓成功\n\n"
                    f"交易对: {symbol}\n"
                    f"订单ID: {result.order_id}\n"
                    f"执行价格: {result.executed_price}\n"
                    f"执行数量: {result.executed_amount}"
                )
            else:
                message = (
                    f"{network_indicator} 平仓失败\n\n"
                    f"错误: {result.error_message}"
                )
            
            await query.message.edit_text(
                message,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")
                ]])
            )
            
        except Exception as e:
            logging.error(f"Error executing confirmed close: {e}")
            await query.answer("执行平仓时发生错误")

    async def monitor_signals(self):
            """监控活动信号"""
            while True:
                try:
                    active_signals = self.db.get_active_signals()
                    for signal in active_signals:
                        # 检查信号状态
                        await self.check_signal_status(signal)
                        
                        # 检查是否需要调整止损
                        if signal.get('dynamic_sl'):
                            await self.check_stop_loss_adjustment(signal)
                            
                        # 检查是否达到多级止盈目标
                        if signal.get('take_profit_levels'):
                            await self.check_take_profit_levels(signal)
                        
                        # 更新统计数据
                        await self.update_signal_statistics(signal)
                        
                except Exception as e:
                    logging.error(f"Error in signal monitoring: {e}")
                    
                await asyncio.sleep(1)

    async def check_take_profit_levels(self, signal: Dict[str, Any]):
        """检查多级止盈目标"""
        try:
            exchange_client = self.exchanges.get(signal['exchange'].upper())
            if not exchange_client:
                return

            # 获取当前价格
            ticker = await exchange_client.get_market_info(signal['symbol'])
            current_price = ticker.last_price

            # logging.info(f"---signal---{signal}")
            # 检查每个止盈级别
            for i, tp_level in enumerate(signal['take_profit_levels']):
                tp_level:TakeProfitLevel
                if tp_level.is_hit:
                    continue

                # 判断是否达到止盈价格
                if (signal['signal_type'] == 'OPEN_LONG' and current_price >= tp_level.price) or \
                   (signal['signal_type'] == 'OPEN_SHORT' and current_price <= tp_level.price):
                    # 执行部分平仓
                    size = signal['position_size'] * tp_level.percentage
                    result = await exchange_client.create_order( 
                        OrderParams(
                        symbol=signal['symbol'],
                        side='sell' if signal['signal_type'] == 'OPEN_LONG' else 'buy',
                        order_type='market',
                        amount=size,
                        extra_params={'reduceOnly': True}
                        )
                    )

                    if result.success:
                        # 更新止盈状态
                        tp_level.is_hit = True
                        tp_level.hit_time = datetime.now()
                        # 记录止盈触发
                        self.db.add_tp_hit(signal['id'], i+1, current_price, size)
                        # 通知用户
                        await self.notify_tp_hit(signal, tp_level, current_price)

        except Exception as e:
            logging.error(f"Error checking take profit levels: {e}")

    async def monitor_risk_metrics(self):
        """监控风险指标"""
        while True:
            try:
                network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
                # 获取账户概览
                overview = await self.exchange_manager.get_account_overview()
                
                # 检查风险指标
                for exchange, data in overview.items():
                    if data['account_health'] in ['WARNING', 'CRITICAL']:
                        await self.notify_risk_warning(network_indicator, exchange, data)
                        
                    # 更新风险统计
                    self.db.update_risk_metrics({
                        'exchange': exchange,
                        'margin_usage': data['margin_ratio'],
                        'total_exposure': data.get('total_exposure', 0),
                        'account_health': data['account_health']
                    })
                    
            except Exception as e:
                logging.error(f"Error in risk monitoring: {e}")
                
            await asyncio.sleep(300)  # 5分钟检查一次

    # main.py 中的 TradingBot 类
    async def check_signal_status(self, signal: Dict[str, Any]):
        """检查信号状态"""
        try:
            orders = self.db.get_signal_orders(signal['id'])
            
            # 修正 entry_zones 的处理
            if signal.get('entry_zones'):
                try:
                    entry_zones = json.loads(signal['entry_zones']) if isinstance(signal['entry_zones'], str) else signal['entry_zones']
                    # 使用字典访问而不是对象属性
                    filled_zones = sum(1 for zone in entry_zones if isinstance(zone, dict) and zone.get('status') == 'FILLED')
                    total_zones = len(entry_zones)
                    
                    if filled_zones == total_zones:
                        await self.notify_full_entry(signal)
                        self.db.update_signal_status(signal['id'], 'ACTIVE')
                except json.JSONDecodeError:
                    logging.error(f"Invalid entry_zones JSON: {signal['entry_zones']}")
            else:
                entry_order = next((o for o in orders if o['order_type'] == 'ENTRY'), None)
                if entry_order and entry_order['status'] == 'FILLED':
                    await self.notify_entry_filled(signal)
                    self.db.update_signal_status(signal['id'], 'ACTIVE')
                    
        except Exception as e:
            logging.error(f"Error checking signal status: {e}")

    async def _check_take_profit_levels(self, exchange_name: str, position: dict):
        """修正止盈目标检查"""
        try:
            if position['size'] == 0:
                return
                
            signal_key = f"{exchange_name}_{position['symbol']}"
            signal = self.active_signals.get(signal_key)
            if not signal:
                return
                
            # 正确处理 take_profit_levels
            try:
                tp_levels = signal.get('take_profit_levels', [])
                if isinstance(tp_levels, str):
                    tp_levels = json.loads(tp_levels)
                
                current_price = position['mark_price']
                
                for tp in tp_levels:
                    # 使用字典访问
                    if tp.get('is_hit'):
                        continue
                        
                    price = float(tp['price'])
                    if signal['action'] == 'OPEN_LONG':
                        if current_price >= price:
                            await self._execute_take_profit(exchange_name, position, tp)
                    else:  # OPEN_SHORT
                        if current_price <= price:
                            await self._execute_take_profit(exchange_name, position, tp)
                            
            except (json.JSONDecodeError, KeyError) as e:
                logging.error(f"Error processing take profit levels: {e}")
                
        except Exception as e:
            logging.error(f"Error checking take profit levels: {e}")

    async def notify_entry_filled(self, signal: Dict[str, Any]):
        """通知入场订单完成"""
        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            message = (
                f"{network_indicator} 入场订单已完成\n\n"
                f"交易对: {signal['symbol']}\n"
                f"方向: {BT.DIRECTION_LONG if signal['action'] == 'OPEN_LONG' else BT.DIRECTION_SHORT}\n"
                f"入场价格: {signal['entry_price']}\n"
                f"数量: {signal['position_size']}\n"
                f"杠杆: {signal['leverage']}x"
            )
            await self.notify_owner(message)
        except Exception as e:
            logging.error(f"Error sending entry notification: {e}")

    async def notify_full_entry(self, signal: Dict[str, Any]):
        """通知区间入场完成"""
        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            message = (
                f"{network_indicator} 区间入场已完成\n\n"
                f"交易对: {signal['symbol']}\n"
                f"方向: {BT.DIRECTION_LONG if signal['action'] == 'OPEN_LONG' else BT.DIRECTION_SHORT}\n\n"
                "入场区间:\n"
            )
            
            for zone in signal['entry_zones']:
                message += f"价格: {zone['price']} ({zone['percentage'] * 100}%)\n"
            
            message += f"\n总数量: {signal['position_size']}\n"
            message += f"杠杆: {signal['leverage']}x"
            
            await self.notify_owner(message)
        except Exception as e:
            logging.error(f"Error sending full entry notification: {e}")

    async def notify_tp_hit(self, signal: Dict[str, Any], tp_level: Dict[str, Any], price: float):
        """通知止盈触发"""
        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            message = (
                f"{network_indicator} 止盈目标已达成\n\n"
                f"交易对: {signal['symbol']}\n"
                f"方向: {BT.DIRECTION_LONG if signal['action'] == 'OPEN_LONG' else BT.DIRECTION_SHORT}\n"
                f"触发价格: {price}\n"
                f"平仓比例: {tp_level['percentage'] * 100}%\n"
                f"止盈级别: {tp_level['level']}/{len(signal['take_profit_levels'])}"
            )
            await self.notify_owner(message)
        except Exception as e:
            logging.error(f"Error sending TP hit notification: {e}")

    async def notify_risk_warning(self, network_indicator: str, exchange: str, data: Dict[str, Any]):
        """发送风险警告"""
        try:
            message = (
                f"{network_indicator} 风险警告!\n\n"
                f"交易所: {exchange}\n"
                f"账户状态: {data['account_health']}\n"
                f"保证金使用率: {data['margin_ratio']:.2f}%\n"
                f"可用保证金: {data['available_margin']:.2f} USDT\n"
                f"未实现盈亏: {data['total_unrealized_pnl']:.2f} USDT"
            )
            
            await self.notify_owner(message)
            
        except Exception as e:
            logging.error(f"Error sending risk warning: {e}")

    async def update_signal_statistics(self, signal: Dict[str, Any]):
        """更新信号统计数据"""
        try:
            exchange = self.exchanges.get(signal['exchange'].upper())
            if not exchange:
                return
                
            position = await exchange.fetch_positions(signal['symbol'])
            if not position:
                return
                
            # 更新统计数据
            stats = {
                'current_pnl': position['unrealized_pnl'],
                'max_profit': position.get('max_profit', 0),
                'max_drawdown': position.get('max_drawdown', 0),
                'holding_time': (datetime.now() - signal['created_at']).total_seconds() / 3600,
                'status': signal['status']
            }
            
            self.db.update_signal_statistics(signal['id'], stats)
            
        except Exception as e:
            logging.error(f"Error updating signal statistics: {e}")

    async def generate_statistics(self) -> dict:
        """生成详细的交易统计"""
        try:
            trades = self.db.get_recent_trades(days=30)
            
            # 计算各种统计数据
            daily_pnl = sum(t['pnl'] for t in trades if t['close_time'].date() == datetime.now().date())
            weekly_pnl = sum(t['pnl'] for t in trades if t['close_time'].date() >= (datetime.now() - timedelta(days=7)).date())
            monthly_pnl = sum(t['pnl'] for t in trades if t['close_time'].date() >= (datetime.now() - timedelta(days=30)).date())
            
            winning_trades = [t for t in trades if t['pnl'] > 0]
            losing_trades = [t for t in trades if t['pnl'] < 0]
            
            stats = {
                'daily_pnl': daily_pnl,
                'weekly_pnl': weekly_pnl,
                'monthly_pnl': monthly_pnl,
                'win_rate': len(winning_trades) / len(trades) * 100 if trades else 0,
                'avg_win': sum(t['pnl'] for t in winning_trades) / len(winning_trades) if winning_trades else 0,
                'avg_loss': sum(t['pnl'] for t in losing_trades) / len(losing_trades) if losing_trades else 0,
                'total_trades': len(trades),
                'winning_trades': len(winning_trades),
                'losing_trades': len(losing_trades),
                'is_testnet': self.config.trading.use_testnet
            }
            
            return stats
            
        except Exception as e:
            logging.error(f"Error generating statistics: {e}")
            return {}
        
        
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /start 命令"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text(
                "未经授权的访问。请联系管理员。"
            )
            return

        network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
        keyboard = [
            [
                InlineKeyboardButton(BT.CHANNEL_MANAGEMENT, callback_data="channel_management"),
                InlineKeyboardButton(BT.TRADE_MANAGEMENT, callback_data="trade_management")
            ],
            [
                InlineKeyboardButton(BT.POSITION_OVERVIEW, callback_data="positions"),
                InlineKeyboardButton(BT.ACCOUNT_STATS, callback_data="account_stats")
            ],
            [
                InlineKeyboardButton(BT.SETTINGS, callback_data="settings"),
                InlineKeyboardButton(BT.HELP, callback_data="help")
            ]
        ]
        
        await update.message.reply_text(
            f"{network_indicator} 欢迎使用交易机器人!\n\n"
            "请从下面的菜单中选择一个选项:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    async def setup_commands(self):
        """设置机器人命令"""
        commands = [
            BotCommand("start", "启动机器人并显示主菜单"),
            BotCommand("help", "显示帮助信息"),
            BotCommand("stats", "查看交易统计"),
            BotCommand("balance", "查看账户余额"),
            BotCommand("positions", "查看当前持仓"),
            BotCommand("channels", "管理监控频道"),
            BotCommand("settings", "机器人设置"),
            BotCommand("cancel", "取消当前操作")
        ]
        await self.application.bot.set_my_commands(commands)

    async def start(self):
        """启动机器人"""
        try:
            # 初始化交易所连接
            if not await self.exchange_manager.initialize():
                logging.error("Failed to initialize exchanges")
                return

            # 启动 Telethon 客户端
            await self.client.start(phone=self.config.PHONE_NUMBER)

            # 注册 Telethon 事件处理器
            @self.client.on(events.NewMessage)
            async def message_handler(event):
                try:
                    await self.handle_channel_message(event)
                except Exception as e:
                    logging.error(f"Error in message handler: {e}")
                    import traceback
                    logging.error(f"Full traceback:\n{traceback.format_exc()}")

            # 启动机器人
            await self.application.initialize()
            await self.application.start()
            await self.application.updater.start_polling()

            # 启动监控任务
            # asyncio.create_task(self.monitor_positions_safely())
            # asyncio.create_task(self.monitor_account_health())
            # asyncio.create_task(self.monitor_signals())
            # asyncio.create_task(self.monitor_risk_metrics())

            # 输出启动信息
            network_type = "测试网" if self.config.trading.use_testnet else "主网"
            logging.info(f"机器人已在{network_type}环境成功启动")
            
            # 通知管理员
            await self.notify_startup()

            # 保持运行
            try:
                await self.client.run_until_disconnected()
            finally:
                await self.stop()

        except Exception as e:
            logging.error(f"Error starting bot: {e}")
            import traceback
            logging.error(f"Full traceback:\n{traceback.format_exc()}")
            raise

    async def notify_startup(self):
        """通知管理员机器人启动状态"""
        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            exchanges_info = []
            
            # 获取各交易所账户信息
            for name, exchange in self.exchange_manager.exchanges.items():
                try:
                    balance = await exchange.fetch_balance()
                    logging.info(f"{name} {exchange} info {balance}")
                    usdt_balance = balance.free_margin
                    exchanges_info.append(f"- {name}: {usdt_balance:.2f} USDT")
                except Exception as e:
                    exchanges_info.append(f"- {name}: 连接错误")

            message = (
                f"{network_indicator} 交易机器人已启动\n\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "交易所连接状态:\n" + "\n".join(exchanges_info) + "\n\n"
                f"运行模式: {'测试网' if self.config.trading.use_testnet else '主网'}\n"
                "监控任务: 已启动\n"
                "信号分析: 已就绪"
            )
            
            await self.notify_owner(message)
        except Exception as e:
            logging.error(f"Error sending startup notification: {e}")

    async def stop(self):
        """停止机器人并清理"""
        try:
            # 通知管理员
            await self.notify_shutdown()
            
            # 停止所有任务
            await self.application.stop()
            await self.client.disconnect()
            
            # 清理数据库连接
            self.db.cleanup()
            
            logging.info("Bot stopped successfully")
        except Exception as e:
            logging.error(f"Error stopping bot: {e}")

    async def notify_shutdown(self):
        """通知管理员机器人关闭"""
        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            message = (
                f"{network_indicator} 交易机器人正在关闭\n\n"
                f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                "正在清理资源...\n"
                "请等待所有任务完成。"
            )
            await self.notify_owner(message)
        except Exception as e:
            logging.error(f"Error sending shutdown notification: {e}")

    def is_authorized(self, user_id: int) -> bool:
        """检查用户是否有权限使用机器人"""
        return user_id == self.config.OWNER_ID

    async def _notify_execute_success(self, signal: TradingSignal, result: OrderResult):
        network_indicator = "🏮 测试网" if self.config.trading.use_testnet else "🔵 主网"
        action_map = {
            'OPEN_LONG': '做多开仓',
            'OPEN_SHORT': '做空开仓',
            'CLOSE': '平仓',
            'UPDATE': '更新',
            'CANCEL': '撤单',
            'TURNOVER': '反手'
        }
        message = (
            f"{network_indicator} 交易执行成功\n\n"
            f"交易所: {signal.exchange}\n"
            f"交易对: {signal.symbol}\n"
            f"动作: {action_map.get(signal.action, signal.action)}\n"
        )
        if result.order_id:
            message += f"订单ID: {result.order_id}\n"
        if result.executed_price is not None:
            message += f"执行价格: {result.executed_price}\n"
        if result.executed_amount is not None:
            message += f"数量: {result.executed_amount}\n"
        await self.notify_owner(message)

    # 修改通知方法以避免 HTML 解析错误
    async def notify_owner(self, message: str):
        """发送通知给管理员"""
        try:
            # 移除可能导致解析错误的HTML标签
            clean_message = message.replace('<', '&lt;').replace('>', '&gt;')
            
            await self.application.bot.send_message(
                chat_id=self.config.OWNER_ID,
                text=clean_message,
                parse_mode=None  # 禁用 HTML 解析
            )
        except Exception as e:
            logging.error(f"Error sending notification to owner: {e}")

    def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理错误"""
        try:
            logging.error(f"Update {update} caused error {context.error}")
            import traceback
            logging.error(f"Full traceback:\n{traceback.format_exc()}")
            
            network_indicator = "🏮 测试网" if self.config.trading.use_testnet else "🔵 主网"
            
            # 尝试发送错误通知
            asyncio.create_task(self.notify_owner(
                f"{network_indicator} 错误报告\n\n"
                f"错误: {context.error}\n"
                f"更新: {update}"
            ))
        except Exception as e:
            logging.error(f"Error in error handler: {e}")
            logging.error(traceback.format_exc())
    async def monitor_positions(self):
        """监控持仓状态"""
        while True:
            try:
                # 获取所有活跃持仓
                for exchange_name, exchange in self.exchange_manager.exchanges.items():
                    positions = await exchange.fetch_positions()  # None表示获取所有持仓
                    
                    for position in positions:
                        if position['size'] == 0:
                            continue
                            
                        # 检查是否需要调整动态止损
                        if self.config.trading.enable_dynamic_sl:
                            await self._check_dynamic_stop_loss(exchange_name, position)
                        
                        # 检查是否触及止盈止损
                        await self._check_take_profit_levels(exchange_name, position)
                        
                        # 更新持仓统计
                        await self._update_position_stats(exchange_name, position)
                
            except Exception as e:
                logging.error(f"Error in position monitoring: {e}")
            
            await asyncio.sleep(1)  # 每秒检查一次

    async def monitor_account_health(self):
        """监控账户健康状态"""
        while True:
            try:
                for exchange_name, exchange in self.exchange_manager.exchanges.items():
                    # 获取账户信息
                    balance = await exchange.fetch_balance()
                    
                    # 计算关键指标
                    total_equity = balance.total_equity
                    used_margin = balance.used_margin
                    available_margin = balance.free_margin
                    margin_ratio = (used_margin / total_equity * 100) if total_equity else 0
                    
                    # 检查风险水平
                    if margin_ratio > 80:  # 危险水平
                        await self.notify_owner(
                            f"⚠️ 危险! {exchange_name} 保证金使用率过高\n"
                            f"当前使用率: {margin_ratio:.2f}%\n"
                            f"可用保证金: {available_margin:.2f} USDT"
                        )
                    elif margin_ratio > 60:  # 警告水平
                        await self.notify_owner(
                            f"⚠️ 警告: {exchange_name} 保证金使用率较高\n"
                            f"当前使用率: {margin_ratio:.2f}%"
                        )
                    
                    # 更新数据库中的账户状态
                    self.db.update_account_status(exchange_name, {
                        'total_equity': total_equity,
                        'used_margin': used_margin,
                        'available_margin': available_margin,
                        'margin_ratio': margin_ratio,
                        'last_update': datetime.now()
                    })
                    
            except Exception as e:
                logging.error(f"Error in account health monitoring: {e}")
            
            await asyncio.sleep(60)  # 每分钟检查一次

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """显示帮助信息"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("未经授权的访问")
            return

        network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
        help_text = f"""
    {network_indicator} 交易机器人使用帮助

    🤖 基本命令:
    /start - 启动机器人
    /help - 显示此帮助信息
    /stats - 查看交易统计
    /balance - 查看账户余额
    /positions - 查看当前持仓
    /channels - 管理监控频道
    /settings - 机器人设置

    📈 交易功能:
    • 自动监控交易信号
    • 手动/自动执行交易
    • 动态止损管理
    • 多级止盈设置

    ⚙️ 设置功能:
    • 配置监控频道
    • 设置交易参数
    • 风险管理设置
    • API配置管理

    📊 统计功能:
    • 交易历史记录
    • 盈亏统计分析
    • 账户健康监控
    """
        await update.message.reply_text(help_text)

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """显示交易统计信息"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("未经授权的访问")
            return

        try:
            # 生成统计信息
            stats = await self.generate_statistics()
            
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            stats_text = f"""
    {network_indicator} 交易统计

    📊 收益统计:
    日收益: {stats['daily_pnl']:.2f} USDT
    周收益: {stats['weekly_pnl']:.2f} USDT
    月收益: {stats['monthly_pnl']:.2f} USDT

    📈 交易数据:
    总交易次数: {stats['total_trades']}
    胜率: {stats['win_rate']:.1f}%
    平均盈利: {stats['avg_win']:.2f} USDT
    平均亏损: {stats['avg_loss']:.2f} USDT

    🎯 详细信息:
    成功交易: {stats['winning_trades']}
    失败交易: {stats['losing_trades']}
    """
            keyboard = [
                [
                    InlineKeyboardButton("详细分析", callback_data="detailed_stats"),
                    InlineKeyboardButton("导出数据", callback_data="export_stats")
                ],
                [InlineKeyboardButton("返回主菜单", callback_data="main_menu")]
            ]
            
            await update.message.reply_text(
                stats_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logging.error(f"Error generating stats: {e}")
            await update.message.reply_text("生成统计信息时发生错误")

    async def balance_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """显示账户余额信息"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("未经授权的访问")
            return

        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            balance_text = f"{network_indicator} 账户余额\n\n"
            
            for exchange_name, exchange in self.exchange_manager.exchanges.items():
                balance = await exchange.fetch_balance()
                
                total_equity = balance.total_equity
                used_margin = balance.used_margin
                available_margin = balance.free_margin
                margin_ratio = (used_margin / total_equity * 100) if total_equity else 0
                
                balance_text += f"📊 {exchange_name}:\n"
                balance_text += f"总权益: {total_equity:.2f} USDT\n"
                balance_text += f"已用保证金: {used_margin:.2f} USDT\n"
                balance_text += f"可用保证金: {available_margin:.2f} USDT\n"
                balance_text += f"保证金率: {margin_ratio:.2f}%\n\n"

            keyboard = [[InlineKeyboardButton("刷新", callback_data="refresh_balance")]]
            
            await update.message.reply_text(
                balance_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logging.error(f"Error fetching balance: {e}")
            await update.message.reply_text("获取账户余额时发生错误")

    async def positions_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """显示当前持仓信息"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("未经授权的访问")
            return

        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            positions_text = f"{network_indicator} 当前持仓\n\n"
            keyboard = []
            
            for exchange_name, exchange in self.exchange_manager.exchanges.items():
                positions = await exchange.fetch_positions()
                active_positions = [p for p in positions if p.size != 0]
                
                if active_positions:
                    positions_text += f"📊 {exchange_name}:\n"
                    for pos in active_positions:
                        direction = "📈 " + BT.DIRECTION_LONG if pos.side == PositionSide.LONG else "📉 "+ BT.DIRECTION_SHORT
                        margin_type = "逐仓" if pos.is_isolated() else "全仓"
                        pnl_emoji = "🟢" if pos.unrealized_pnl > 0 else "🔴"
                        
                        positions_text += (
                            f"\n{pos.symbol}: {direction} | {margin_type}\n"
                            f"持仓数量: {abs(pos.size):.4f}\n"
                            f"开仓价格: {pos.entry_price:.6f}\n"
                            f"标记价格: {pos.mark_price:.6f}\n"
                            f"保本价格: {pos.break_even_price:.6f}\n"
                            f"清算价格: {pos.liquidation_price:.6f}\n"
                            f"杠杆倍数: {pos.leverage}x\n"
                            f"\n保证金信息:\n"
                            f"初始保证金: {pos.initial_margin:.4f} USDT\n"
                            f"- 持仓保证金: {pos.position_initial_margin:.4f} USDT\n"
                            f"- 委托保证金: {pos.open_order_initial_margin:.4f} USDT\n"
                            f"维持保证金: {pos.maintenance_margin:.4f} USDT\n"
                            f"可用保证金: {pos.collateral:.4f} USDT\n"
                            f"\n盈亏信息: {pnl_emoji}\n"
                            f"未实现盈亏: {pos.unrealized_pnl:.4f} USDT ({pos.pnl_percentage:.2f}%)\n"
                            f"已实现盈亏: {pos.realized_pnl:.4f} USDT\n"
                            f"名义价值: {pos.notional:.4f} USDT\n"
                            f"\n————————————\n"
                        )
                        
                        # 添加每个持仓的操作按钮
                        keyboard.append([
                            InlineKeyboardButton(
                                f"修改 {pos.symbol}",
                                callback_data=f"modify_{exchange_name}_{pos.symbol}"
                            ),
                            InlineKeyboardButton(
                                f"平仓 {pos.symbol}",
                                callback_data=f"close_{exchange_name}_{pos.symbol}"
                            )
                        ])

            if not keyboard:
                positions_text += "当前没有持仓"
            else:
                keyboard.append([InlineKeyboardButton("刷新", callback_data="refresh_positions")])
            
            await update.message.reply_text(
                positions_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            
        except Exception as e:
            logging.error(f"Error fetching positions: {e}")
            await update.message.reply_text("获取持仓信息时发生错误")

    async def _check_dynamic_stop_loss(self, exchange_name: str, position: dict):
        """检查并更新动态止损"""
        try:
            if position['size'] == 0:
                return
                
            # 获取对应的信号
            signal_key = f"{exchange_name}_{position['symbol']}"
            signal = self.active_signals.get(signal_key)
            if not signal or not signal.dynamic_sl:
                return
                
            # 计算新的止损价格
            entry_price = position['entry_price']
            current_price = position['mark_price']
            
            if signal.action == 'OPEN_LONG':
                if current_price > entry_price:
                    profit_distance = current_price - entry_price
                    new_sl = entry_price + (profit_distance * 0.5)  # 设置在50%利润位置
                    if new_sl > signal.stop_loss:
                        await self.exchange_manager.modify_position(
                            exchange_name,
                            position['symbol'],
                            new_sl=new_sl
                        )
            else:  # OPEN_SHORT
                if current_price < entry_price:
                    profit_distance = entry_price - current_price
                    new_sl = entry_price - (profit_distance * 0.5)
                    if new_sl < signal.stop_loss:
                        await self.exchange_manager.modify_position(
                            exchange_name,
                            position['symbol'],
                            new_sl=new_sl
                        )
                        
        except Exception as e:
            logging.error(f"Error checking dynamic stop loss: {e}")


    async def _execute_take_profit(self, exchange_name: str, position: dict, tp_level):
        """执行止盈"""
        try:
            # 计算平仓数量
            close_amount = abs(position['size']) * tp_level.percentage
            
            result = await self.exchange_manager.place_order(
                exchange=exchange_name,
                symbol=position['symbol'],
                side='sell' if position['size'] > 0 else 'buy',
                type='MARKET',
                amount=close_amount,
                params={'reduceOnly': True}
            )
            
            if result.success:
                tp_level.is_hit = True
                tp_level.hit_time = datetime.now()
                
                # 发送通知
                await self.notify_owner(
                    f"🎯 止盈触发: {position['symbol']}\n"
                    f"价格: {result.executed_price}\n"
                    f"数量: {result.executed_amount}\n"
                    f"级别: TP{tp_level.level}"
                )
                
        except Exception as e:
            logging.error(f"Error executing take profit: {e}")

    async def _update_position_stats(self, exchange_name: str, position: dict):
        """更新持仓统计数据"""
        try:
            stats = {
                'exchange': exchange_name,
                'symbol': position['symbol'],
                'size': position['size'],
                'entry_price': position['entry_price'],
                'current_price': position['mark_price'],
                'unrealized_pnl': position.get('unrealized_pnl', 0),
                'margin_ratio': position.get('margin_ratio', 0),
                'leverage': position.get('leverage', 0),
                'liquidation_price': position.get('liquidation_price', 0),
                'last_update': datetime.now()
            }
            
            # 计算持仓时间
            signal_key = f"{exchange_name}_{position['symbol']}"
            signal = self.active_signals.get(signal_key)
            if signal:
                stats['holding_time'] = (datetime.now() - signal.timestamp).total_seconds() / 3600
                
                # 计算最大回撤和最大收益
                initial_value = abs(position['size']) * position['entry_price']
                current_value = abs(position['size']) * position['mark_price']
                
                if signal.action == 'OPEN_LONG':
                    profit_percentage = (current_value - initial_value) / initial_value * 100
                else:  # OPEN_SHORT
                    profit_percentage = (initial_value - current_value) / initial_value * 100
                    
                # 更新历史最大值
                if profit_percentage > signal.additional_info.get('max_profit', 0):
                    signal.additional_info['max_profit'] = profit_percentage
                
                # 更新历史最大回撤
                if profit_percentage < signal.additional_info.get('max_drawdown', 0):
                    signal.additional_info['max_drawdown'] = profit_percentage
                
                stats['max_profit'] = signal.additional_info.get('max_profit', 0)
                stats['max_drawdown'] = signal.additional_info.get('max_drawdown', 0)
            
            # 更新数据库
            self.db.update_position_stats(stats)
            
            # 检查是否需要发送风险警告
            await self._check_position_risks(exchange_name, position, stats)
            
        except Exception as e:
            logging.error(f"Error updating position stats: {e}")

    async def _check_position_risks(self, exchange_name: str, position: dict, stats: dict):
        """检查持仓风险"""
        try:
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            warnings = []
            
            # 检查清算风险
            if position.get('margin_ratio', 0) > 80:
                warnings.append(f"⚠️ 保证金率过高: {position['margin_ratio']:.1f}%")
            
            # 检查大幅亏损
            if position.get('unrealized_pnl', 0) < 0:
                loss_percentage = abs(position['unrealized_pnl']) / (position['size'] * position['entry_price']) * 100
                if loss_percentage > 20:  # 亏损超过20%
                    warnings.append(f"📉 大幅亏损: {loss_percentage:.1f}%")
            
            # 检查持仓时间
            if stats.get('holding_time', 0) > 48:  # 持仓超过48小时
                warnings.append(f"⏰ 长期持仓: {stats['holding_time']:.1f}小时")
            
            # 如果有任何警告，发送通知
            if warnings:
                message = (
                    f"{network_indicator} 持仓风险警告\n\n"
                    f"交易对: {position['symbol']}\n"
                    f"交易所: {exchange_name}\n\n"
                    "警告项目:\n" + "\n".join(warnings) + "\n\n"
                    f"建议采取行动管理风险"
                )
                await self.notify_owner(message)
            
        except Exception as e:
            logging.error(f"Error checking position risks: {e}")


    async def show_account_stats(self, message):
        """显示账户统计信息"""
        try:
            # 获取统计数据
            stats = await self.generate_statistics()
            
            network_indicator = BT.TESTNET_INDICATOR if self.config.trading.use_testnet else BT.MAINNET_INDICATOR
            
            # 生成统计信息文本
            stats_text = (
                f"{network_indicator} 账户统计\n\n"
                f"📈 交易表现\n"
                f"总交易次数: {stats.get('total_trades', 0)}\n"
                f"成功交易: {stats.get('winning_trades', 0)}\n"
                f"失败交易: {stats.get('losing_trades', 0)}\n"
                f"胜率: {stats.get('win_rate', 0):.2f}%\n\n"
                
                f"💰 收益统计\n"
                f"日收益: {stats.get('daily_pnl', 0):.2f} USDT\n"
                f"周收益: {stats.get('weekly_pnl', 0):.2f} USDT\n"
                f"月收益: {stats.get('monthly_pnl', 0):.2f} USDT\n\n"
                
                f"📊 交易分析\n"
                f"平均盈利: {stats.get('avg_win', 0):.2f} USDT\n"
                f"平均亏损: {stats.get('avg_loss', 0):.2f} USDT\n"
                f"最大单笔盈利: {stats.get('max_win', 0):.2f} USDT\n"
                f"最大单笔亏损: {stats.get('max_loss', 0):.2f} USDT\n"
            )

            # 创建操作按钮
            keyboard = [
                [
                    InlineKeyboardButton("详细分析", callback_data="detailed_stats"),
                    InlineKeyboardButton("导出数据", callback_data="export_stats")
                ],
                [InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")]
            ]

            await message.edit_text(
                stats_text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

        except Exception as e:
            logging.error(f"Error showing account stats: {e}")
            await message.edit_text(
                "获取统计信息时发生错误",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(BT.BACK_MAIN, callback_data="main_menu")
                ]])
            )
            
            
    # 新增处理方法
    async def _handle_channels_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """处理 /channels 命令"""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("未经授权的访问")
            return
        
        # 调用 channel_management 的显示方法，指定这是新消息
        await self.channel_management.show_channel_management(update.message, is_new_message=True)
        
        
        
    # 4. 添加辅助函数用于持仓监控
    async def monitor_positions_safely(self):
        """安全的持仓监控实现"""
        while True:
            try:
                for exchange_name, exchange in self.exchange_manager.exchanges.items():
                    try:
                        positions = await exchange.fetch_positions()
                        for position in positions:
                            if position.get('size', 0) == 0:
                                continue
                                
                            # 检查动态止损
                            # if self.exchange_manager.config.trading.enable_dynamic_sl:
                            #     await self.exchange_manager._check_dynamic_stop_loss(
                            #         exchange_name, 
                            #         position
                            #     )
                            
                            # # 检查止盈目标
                            # await self.exchange_manager._check_take_profits(
                            #     exchange_name, 
                            #     position
                            # )
                            
                            # # 更新统计数据
                            # await self.exchange_manager._update_position_stats(
                            #     exchange_name, 
                            #     position
                            # )
                    except Exception as e:
                        logging.error(f"Error monitoring positions for {exchange_name}: {e}")
                        continue
                        
            except Exception as e:
                logging.error(f"Error in position monitoring: {e}")
                
            await asyncio.sleep(1)  # 每秒检查一次
        
        
async def main():
    """主函数"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('bot.log')
        ]
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    try:
        # 初始化配置
        config = Config()
        
        # 输出运行模式
        network_type = "测试网" if config.trading.use_testnet else "主网"
        logging.info(f"正在启动机器人 (运行模式: {network_type})")
        
        # 创建机器人实例
        bot = TradingBot(config)
        
        # 添加重试机制
        max_retries = 3
        retry_count = 0
        
        while retry_count < max_retries:
            try:
                # 启动机器人
                await bot.start()
                break
            except Exception as e:
                retry_count += 1
                if retry_count == max_retries:
                    logging.error(f"Bot startup failed after {max_retries} attempts: {e}")
                    raise
                    
                logging.warning(f"Retry {retry_count}: {e}")
                await asyncio.sleep(2 ** retry_count)
        
    except Exception as e:
        logging.error("Critical error during startup:", exc_info=True)
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Bot stopped by user")
    except Exception as e:
        logging.error("Fatal error:", exc_info=True)
        sys.exit(1)
