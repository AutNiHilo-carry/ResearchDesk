# PostgreSQL 18 MVCC：选型摘记

这是 ResearchDesk 根据 PostgreSQL 18 官方文档编写的局部中文摘记，不是网页完整快照。核对日期：2026-11-16。版本范围：18；未核对 17 版对应页面。

PostgreSQL 18 使用 MVCC，以数据快照管理并发访问。
文档所述 MVCC 的读写锁互不冲突，不能推广为所有操作、所有锁或两个写者均不会阻塞。
PostgreSQL 还提供表锁、行锁和应用定义的 advisory locks。
这份摘记没有特定业务的 TPS、P99 延迟测量，也没有外部 API 副作用的 exactly-once 协议。

来源：https://www.postgresql.org/docs/18/mvcc-intro.html
