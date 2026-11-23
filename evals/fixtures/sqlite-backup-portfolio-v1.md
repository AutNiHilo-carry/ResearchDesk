# SQLite 在线备份：项目中文摘记

来源：https://www.sqlite.org/backup.html ，章节 Using the SQLite Online Backup API / Other Backup Techniques。
核对日期：2026-11-23。以下为项目转述，不是完整原文；未固定SQLite版本，未运行数据库实验。

Online Backup API可分步将在线源数据库复制到目标库；分步模式只在读取时短暂锁定源库，不必全程锁住。
成功完成备份会得到源数据库快照。VACUUM INTO也可将在线数据库复制到另一文件。
此摘记没有测量备份延迟、锁等待或断电恢复率。
