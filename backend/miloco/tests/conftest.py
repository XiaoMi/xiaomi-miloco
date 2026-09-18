def inject_test_kv_repo(monkeypatch) -> None:
    """给只挂 router、不过 lifespan 的端到端测试补一份绑定到本用例 MILOCO_HOME
    的 KVRepo(须在 fixture 设置好 MILOCO_HOME 之后调用)。
    manager._kv_repo 平时由 Manager.initialize() 灌注;用 monkeypatch 而非裸赋值:
    退出时自动还原成进入前的值(而不是一律拍成 None,把 None 泄漏给后续用例)。
    """
    import miloco.database.connector as _connector_module
    from miloco.admin.router import manager as _manager
    from miloco.database.kv_repo import KVRepo as _KVRepo

    monkeypatch.setattr(_connector_module, "db_connector", None)
    _connector_module.init_database()
    monkeypatch.setattr(_manager, "_kv_repo", _KVRepo(), raising=False)
