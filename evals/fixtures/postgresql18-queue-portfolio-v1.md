# PostgreSQL 18 队列领取：项目中文摘记

来源：https://www.postgresql.org/docs/18/sql-select.html ，章节 The Locking Clause。
核对日期：2026-11-23。范围仅PostgreSQL 18；以下为项目转述，不是完整原文，未运行并发测试。

SKIP LOCKED会跳过不能立即获得行锁的选中行。
这种跳过会产生不一致的数据视图，不适合一般查询，但可减少多个消费者访问队列表时的锁竞争。
该选项只作用于行锁，所需的ROW SHARE表锁仍按通常方式获取。
本摘记没有给出吞吐测量、严格FIFO或外部业务副作用恰好一次的保证。
