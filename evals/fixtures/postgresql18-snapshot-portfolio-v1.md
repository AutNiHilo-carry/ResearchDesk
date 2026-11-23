# PostgreSQL 18 查询快照：项目中文摘记

来源：https://www.postgresql.org/docs/18/transaction-iso.html ，章节 Read Committed Isolation Level。
核对日期：2026-11-23。范围仅PostgreSQL 18；以下为项目转述，不是完整原文，未运行SQL实验。

Read Committed下，普通SELECT看到查询开始前已提交的数据，即该条查询开始时的快照。
同一事务中连续两条SELECT之间，若其他事务提交了变化，两条SELECT可能看到不同数据。
以上不对SELECT FOR UPDATE/SHARE的行为作完整说明，也不是所有隔离级别的共同保证。
