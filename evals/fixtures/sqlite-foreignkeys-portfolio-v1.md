# SQLite 外键开关：项目中文摘记

来源：https://www.sqlite.org/foreignkeys.html ，章节 Enabling Foreign Key Support。
核对日期：2026-11-23。以下为项目转述，不是完整原文；未固定SQLite版本或编译选项，未运行SQL实验。

应用应逐个数据库连接明确配置外键开关，不假定默认开启或关闭；可用PRAGMA foreign_keys查询当前状态。
在多语句事务中间切换外键开关不会报错，但不会生效。
因此仅有外键DDL，不能替代对实际连接配置和运行环境的核查。
