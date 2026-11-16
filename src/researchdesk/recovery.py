"""Use LangGraph's SQLite checkpointer; own only application recovery decisions."""
from langgraph.checkpoint.sqlite import SqliteSaver


def run_graph(ledger, initial_input, limits, model_name, *, builder=None, allowed_tools=None, thread_id=None):
    if builder is None:
        from .agent import build_agent
        builder = build_agent
    config = {"configurable": {"thread_id": thread_id or ledger.directory.name},
              "recursion_limit": 60, "max_concurrency": 1}
    with SqliteSaver.from_conn_string(str(ledger.directory / "checkpoints.sqlite3")) as saver:
        saver.conn.execute("PRAGMA synchronous=FULL")
        options = {} if allowed_tools is None else {"allowed_tools": allowed_tools}
        graph = builder(ledger, limits=limits, checkpointer=saver, model_name=model_name, **options)
        checkpoint = saver.get_tuple(config)
        if checkpoint is not None:
            state = graph.get_state(config)
            if not state.next:
                # Graph finished but report/manifest may not have been exported before crash.
                return state.values
            return graph.invoke(None, config=config, durability="sync")
        # A kill before the first durable checkpoint is safe to start from prepared input.
        return graph.invoke(initial_input, config=config, durability="sync")

