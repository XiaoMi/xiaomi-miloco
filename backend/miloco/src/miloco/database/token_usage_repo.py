"""Token usage event log repo — SQLite storage with daily rollup retention.

Schema:
  - token_usage         : live event rows (last 3 days), one row per API call
  - token_usage_daily   : per-day rollup keyed by (date, model, base_url, type),
                          preserved across retention window so historical trend /
                          model / type breakdown stay queryable

模型身份 = (model, base_url)。同一个模型名可以同时挂在两个 endpoint 上，故两张表都
带 base_url、且它进日表主键。存的是**完整 URL 原文**——差异可能落在 URL 的任何位置
（主机、路径、端口），截断是展示层的事。``''`` = 该行早于本列引入（v3 之前），来源
未记录；**永不回填**，展示侧直接说「旧版本数据未记录 URL」。

Field semantics:
  - input_tokens   = prompt_tokens (total input, all modalities)
  - cache_tokens   = prompt_tokens_details.cached_tokens   (⊆ input_tokens)
  - video_tokens   = prompt_tokens_details.video_tokens    (⊆ input_tokens)
  - audio_tokens   = prompt_tokens_details.audio_tokens    (⊆ input_tokens)
  - output_tokens  = completion_tokens
Derivations (no need to store):
  - text_tokens     = input - video - audio
  - billable_tokens = input - cache

Retention: on first insert of each day, events older than 3 days are aggregated
by (date, model, type) into the daily table via INSERT...SELECT...GROUP BY +
ON CONFLICT UPSERT, then deleted from the live table. The whole operation runs
in a single explicit transaction (BEGIN/COMMIT) — the connector is in autocommit
mode, so we must start a transaction explicitly for atomicity.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta

from miloco.database.connector import get_db_connector

logger = logging.getLogger(__name__)

_RETENTION_DAYS = 3
_DEFAULT_EVENT_LIMIT = 100000  # ample headroom for the 3-day retention window


class TokenUsageRepo:
    """Data access object for token_usage + token_usage_daily."""

    def __init__(self) -> None:
        self.db = get_db_connector()
        self._last_archive_check: date | None = None

    def insert(self, model: str, base_url: str, usage: dict, type: str) -> None:
        """Insert one event. Triggers rollup on first call of each new day.

        `type` is either ``"realtime"`` (perception-loop driven) or
        ``"on_demand"`` (user-initiated query). ``base_url`` 存完整原文；空串表示
        调用方拿不到（正常路径不会发生，见 fire_record 的三个调用点）。
        """
        ts_ms = int(time.time() * 1000)
        today = datetime.fromtimestamp(ts_ms / 1000).date()
        if self._last_archive_check != today:
            # Only mark "done for today" after rollup succeeds. Otherwise a
            # persistent failure (disk full, lock contention) would be masked:
            # the flag would skip retries all day while the live table grows.
            self._maybe_rollup(ts_ms)
            self._last_archive_check = today

        details = usage.get("prompt_tokens_details") or {}
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        cache_tokens = details.get("cached_tokens") or 0
        video_tokens = details.get("video_tokens") or 0
        audio_tokens = details.get("audio_tokens") or 0

        with self.db.get_connection() as conn:
            conn.execute(
                "INSERT INTO token_usage "
                "(timestamp, model, base_url, type, input_tokens, output_tokens, "
                " cache_tokens, video_tokens, audio_tokens, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts_ms, model, base_url or "", type,
                    input_tokens, output_tokens,
                    cache_tokens, video_tokens, audio_tokens,
                    ts_ms,
                ),
            )
            conn.commit()

    def clear_all(self) -> dict[str, int | str | None]:
        """删除全部 token 用量(实时表 + 日 rollup),返回各表删除行数。

        供 admin 重置统计用(不可恢复)。两表在同一事务内一并清空。
        """
        return self.clear_since(None)

    def clear_since(
        self,
        since_ms: int | None,
        model: str | None = None,
        base_url: str | None = None,
        from_date: str | None = None,
    ) -> dict[str, int | str | None]:
        """删除用量记录。三个条件都是可选的，同时生效（AND）。

        - ``since_ms``：该时刻及其之后；``None`` = 不限时间
        - ``model`` / ``base_url``：限定到某一个「模型名 + endpoint」；``None`` = 不限

        ⚠️ ``model`` 与 ``base_url`` **必须同时给或同时不给**。模型的唯一身份是这两者
        的组合，只给模型名会跨掉它的所有 endpoint——那不是任何界面入口的语义，
        真发生了几乎一定是调用方的 bug，与其多删不如直接报错。

        ⚠️ ``base_url=""`` 是**有意义的取值**（schema v3 之前的老数据，来源未记录），
        所以判空一律用 ``is not None``、不能用真值判断——用真值判断会让「删这批
        老数据」静默变成「不限 endpoint 全删」。

        为什么两表都要动、且必须同一事务：同一段时间的数据可能一半还在实时表、
        一半已经 rollup 进日表（分界是 _RETENTION_DAYS）。只清一张会留下半清状态——
        界面上表现为总量与明细对不上，而且**不会有任何报错**。

        ⚠️ 日表的粒度是**天**，没有更细的时间戳。所以按「近 24 小时」这类跨天范围
        删除时，日表只能按「since 所在那一天」整天删——这会连带删掉 since 之前、
        但落在同一天里的记录。这是日聚合本身的精度损失，SQL 绕不过去：那些行的
        原始时间戳在 rollup 时就已经不存在了。故额外返回 ``daily_from_date``，
        让调用方能把这件事说清楚，而不是悄悄多删。

        ⚠️ ``from_date`` 存在的唯一理由：**界面承诺的那一天必须就是真被删的那一天。**
        日表的 ``date`` 是按**本机时区**写进去的，而界面上那句「某天更早的记录会被
        连带删除」是浏览器按**它自己的时区**算的。盒子跑 UTC、手机在 +08 时两者能差
        一天，且差错的方向可能是「实际删的比说的更多」——那正是这句提示要防的事。
        故允许调用方把它已经显示给用户的那一天传进来，由它来定日表的边界：
        说了哪天就删哪天。为防这个入口被用来任意扩大范围，只接受与本机推算相差
        不超过一天的日期，超出即报错。
        """
        if from_date is not None:
            try:
                given = date.fromisoformat(from_date)
            except ValueError as e:
                raise ValueError(f"from_date 需要 YYYY-MM-DD，收到 {from_date!r}") from e
            if since_ms is None:
                raise ValueError("from_date 只在给了 since_ms 时才有意义")
            derived = datetime.fromtimestamp(since_ms / 1000).date()
            if abs((given - derived).days) > 1:
                raise ValueError(
                    f"from_date {from_date} 与 since_ms 推算的 {derived.isoformat()} "
                    "相差超过一天，只接受时区差那一天的偏移"
                )

        if (model is None) != (base_url is None):
            raise ValueError(
                "model 与 base_url 必须同时给或同时不给"
                f"（收到 model={model!r} base_url={base_url!r}）"
            )

        cond_live: list[str] = []
        cond_daily: list[str] = []
        p_live: list = []
        p_daily: list = []

        if since_ms is not None:
            # 调用方给了就用它的（界面已经把这一天写给用户看了），否则按本机推算
            if from_date is None:
                from_date = datetime.fromtimestamp(since_ms / 1000).date().isoformat()
            cond_live.append("timestamp >= ?")
            p_live.append(since_ms)
            cond_daily.append("date >= ?")
            p_daily.append(from_date)

        if model is not None:
            # base_url 同为非 None（上面已断言），空串在此按值精确匹配
            for cond, params in ((cond_live, p_live), (cond_daily, p_daily)):
                cond.append("model = ?")
                params.append(model)
                cond.append("base_url = ?")
                params.append(base_url)

        where_live = (" WHERE " + " AND ".join(cond_live)) if cond_live else ""
        where_daily = (" WHERE " + " AND ".join(cond_daily)) if cond_daily else ""

        with self.db.get_connection() as conn:
            conn.execute("BEGIN")
            try:
                n_live = conn.execute(
                    "SELECT COUNT(*) FROM token_usage" + where_live, tuple(p_live)
                ).fetchone()[0]
                n_daily = conn.execute(
                    "SELECT COUNT(*) FROM token_usage_daily" + where_daily,
                    tuple(p_daily),
                ).fetchone()[0]
                conn.execute("DELETE FROM token_usage" + where_live, tuple(p_live))
                conn.execute(
                    "DELETE FROM token_usage_daily" + where_daily, tuple(p_daily)
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "token_usage": int(n_live),
            "token_usage_daily": int(n_daily),
            "daily_from_date": from_date,
        }

    def list_events(
        self,
        since_ms: int | None = None,
        until_ms: int | None = None,
        limit: int = _DEFAULT_EVENT_LIMIT,
    ) -> tuple[list[dict], bool]:
        """Return raw events in [since_ms, until_ms]. Returns (rows, truncated).

        Defaults: since=today 00:00 local, until=now. ``limit`` caps the result
        so callers can't accidentally pull tens of MB; ``truncated=True`` signals
        that more rows exist beyond the cap (caller should narrow the window).
        """
        if since_ms is None:
            since_ms = int(
                datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000
            )
        if until_ms is None:
            until_ms = int(time.time() * 1000)
        # Fetch limit+1 so we can detect overflow without an extra COUNT.
        with self.db.get_connection() as conn:
            rows = conn.execute(
                "SELECT timestamp, model, base_url, type, input_tokens, "
                "       output_tokens, cache_tokens, video_tokens, audio_tokens "
                "FROM token_usage WHERE timestamp BETWEEN ? AND ? "
                "ORDER BY timestamp ASC LIMIT ?",
                (since_ms, until_ms, limit + 1),
            ).fetchall()
        truncated = len(rows) > limit
        if truncated:
            rows = rows[:limit]
        return [dict(r) for r in rows], truncated

    def aggregate_buckets(
        self,
        since_ms: int | None = None,
        until_ms: int | None = None,
        bin_minutes: int = 60,
    ) -> list[dict]:
        """Bucketed aggregation of raw events in [since_ms, until_ms], grouped by
        (time bucket, model, base_url, type). ``bin_minutes`` is the bucket width.

        base_url 也要进 GROUP BY：「今日」视图的明细行是从这个接口来的（week/month
        走 aggregate_daily），漏了它会导致今日能按 endpoint 分、近 7 天不能。

        Used by the "today" view: the response size is bounded by bucket count
        (≈ day / bin × models × types), not by event count, so it never hits the
        raw-event cap regardless of activity. Returns rows with ``bucket_ms``
        (bucket start, ms epoch) plus per-bucket sums.
        """
        if since_ms is None:
            since_ms = int(
                datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000
            )
        if until_ms is None:
            until_ms = int(time.time() * 1000)
        bin_ms = max(1, bin_minutes) * 60_000
        with self.db.get_connection() as conn:
            # CAST(... AS INTEGER) 截断为整数桶下标；timestamp ≥ since 保证非负 = floor。
            rows = conn.execute(
                "SELECT CAST((timestamp - ?) / ? AS INTEGER) AS bkt, model, "
                "       base_url, type, "
                "       COUNT(*) AS calls, "
                "       SUM(input_tokens) AS input_tokens, "
                "       SUM(output_tokens) AS output_tokens, "
                "       SUM(cache_tokens) AS cache_tokens, "
                "       SUM(video_tokens) AS video_tokens, "
                "       SUM(audio_tokens) AS audio_tokens "
                "FROM token_usage WHERE timestamp BETWEEN ? AND ? "
                "GROUP BY bkt, model, base_url, type "
                "ORDER BY bkt, model, base_url, type",
                (since_ms, bin_ms, since_ms, until_ms),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            bkt = d.pop("bkt")
            d["bucket_ms"] = since_ms + bkt * bin_ms
            out.append(d)
        return out

    def aggregate_daily(
        self, since: str | None = None, until: str | None = None
    ) -> list[dict]:
        """Return per-day rollup rows. `since` / `until` are inclusive YYYY-MM-DD.

        Combines `token_usage_daily` (historical) with on-the-fly aggregation of
        `token_usage` (live, last 3 days) so the caller sees a uniform daily view.
        """
        conditions: list[str] = []
        params: list = []
        if since:
            conditions.append("date >= ?")
            params.append(since)
        if until:
            conditions.append("date <= ?")
            params.append(until)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        with self.db.get_connection() as conn:
            historical = conn.execute(
                f"SELECT date, model, base_url, type, calls, input_tokens, "
                f"       output_tokens, cache_tokens, video_tokens, audio_tokens "
                f"FROM token_usage_daily {where} "
                f"ORDER BY date ASC, model, base_url, type",
                params,
            ).fetchall()

            live = conn.execute(
                f"SELECT date(timestamp / 1000, 'unixepoch', 'localtime') AS date, "
                f"  model, base_url, type, "
                f"  COUNT(*) AS calls, "
                f"  SUM(input_tokens) AS input_tokens, "
                f"  SUM(output_tokens) AS output_tokens, "
                f"  SUM(cache_tokens) AS cache_tokens, "
                f"  SUM(video_tokens) AS video_tokens, "
                f"  SUM(audio_tokens) AS audio_tokens "
                f"FROM token_usage "
                f"GROUP BY date, model, base_url, type "
                f"{('HAVING ' + ' AND '.join(conditions)) if conditions else ''} "
                f"ORDER BY date ASC, model, base_url, type",
                params,
            ).fetchall()

        return [dict(r) for r in historical] + [dict(r) for r in live]

    def _maybe_rollup(self, now_ms: int) -> None:
        """Roll up events older than _RETENTION_DAYS into token_usage_daily.

        SQL does the GROUP BY internally via INSERT...SELECT...ON CONFLICT,
        then a DELETE prunes the live table. Both wrapped in one transaction.

        ⚠️ base_url 必须同时出现在 GROUP BY 与 ON CONFLICT 里。漏掉任何一处，两个
        endpoint 的同日数据都会被**静默累加成一行**，而紧接着的 DELETE 会把原始行
        删掉——不可恢复，也不会有任何报错。有专门用例钉住这一条。
        """
        # Day-aligned cutoff: a day is either fully rolled up or fully raw,
        # never split. Otherwise aggregate_daily() would return two rows for
        # the boundary day (one from each table) sharing the same key.
        today = datetime.fromtimestamp(now_ms / 1000).date()
        cutoff_date = today - timedelta(days=_RETENTION_DAYS)
        cutoff_ms = int(
            datetime.combine(cutoff_date, datetime.min.time()).timestamp() * 1000
        )
        with self.db.get_connection() as conn:
            exists = conn.execute(
                "SELECT 1 FROM token_usage WHERE timestamp < ? LIMIT 1",
                (cutoff_ms,),
            ).fetchone()
            if not exists:
                return

            conn.execute("BEGIN")
            try:
                conn.execute(
                    """
                    INSERT INTO token_usage_daily
                        (date, model, base_url, type, calls,
                         input_tokens, output_tokens,
                         cache_tokens, video_tokens, audio_tokens)
                    SELECT
                        date(timestamp / 1000, 'unixepoch', 'localtime') AS d,
                        model, base_url, type,
                        COUNT(*),
                        SUM(input_tokens), SUM(output_tokens),
                        SUM(cache_tokens), SUM(video_tokens), SUM(audio_tokens)
                    FROM token_usage
                    WHERE timestamp < ?
                    GROUP BY d, model, base_url, type
                    ON CONFLICT(date, model, base_url, type) DO UPDATE SET
                        calls = calls + excluded.calls,
                        input_tokens = input_tokens + excluded.input_tokens,
                        output_tokens = output_tokens + excluded.output_tokens,
                        cache_tokens = cache_tokens + excluded.cache_tokens,
                        video_tokens = video_tokens + excluded.video_tokens,
                        audio_tokens = audio_tokens + excluded.audio_tokens
                    """,
                    (cutoff_ms,),
                )
                deleted = conn.execute(
                    "DELETE FROM token_usage WHERE timestamp < ?", (cutoff_ms,)
                ).rowcount
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            logger.info(
                "rolled up %d events older than %d ms into token_usage_daily",
                deleted,
                cutoff_ms,
            )


_repo: TokenUsageRepo | None = None


def get_token_usage_repo() -> TokenUsageRepo:
    """Singleton accessor."""
    global _repo
    if _repo is None:
        _repo = TokenUsageRepo()
    return _repo


def fire_record(model: str, base_url: str, usage: dict, type: str) -> None:
    """Record one omni usage event (synchronous direct insert).

    历史教训:曾用 ``asyncio.create_task`` 排到当前 loop 异步写,但感知主路径每窗
    跑在 inference 线程的临时 loop(``asyncio.run`` 起的)上,窗末 ``asyncio.run``
    退出会把该 task ``cancel``;``CancelledError`` 是 ``BaseException`` 又躲过了
    ``except Exception`` → realtime 用量在 Linux(fsync 真落盘、insert 慢)上几乎必丢、
    macOS(fsync 近 no-op)上几乎必中,开发自测全绿、生产静默丢数据。

    现在直接同步 insert:omni 调用本身 8-12s,一次 sqlite insert(~数十 ms)完全无感,
    且 loop 归属无关、Mac/Linux 行为一致、不会被 cancel。同步路径里不再有 await 点,
    不可能产生 ``CancelledError``,故只兜 ``Exception``(含 sqlite lock / disk full 的
    ``sqlite3.OperationalError``)——用量记录永不把异常抛进 omni 请求路径;
    ``KeyboardInterrupt`` / ``SystemExit`` 仍照常向上传播,不被静默吞掉。
    """
    try:
        get_token_usage_repo().insert(model, base_url, usage, type)
    except Exception as e:  # noqa: BLE001
        logger.warning("usage log failed: %s", e)
