;

const NODE_W = 200;
const NODE_H = 64;
const SCROLL_STORAGE_KEY = 'dag-task-runner:scroll-y';
const COMPLETED_DOT_COLOR = '#22c55e';

function pillToneFor(status: TaskStatus): 'neutral' | 'info' | 'success' | 'warning' {
  switch (status) {
    case 'PENDING':
      return 'neutral';
    case 'RUNNING':
      return 'info';
    case 'FINISHED':
      return 'success';
    case 'ERROR':
      return 'warning';
  }
}

function complexityTone(c: Complexity): 'neutral' | 'info' | 'warning' {
  switch (c) {
    case 'HIGH':
      return 'warning';
    case 'MED':
      return 'info';
    case 'LOW':
      return 'neutral';
  }
}

function formatDuration(ms?: number): string {
  if (ms === undefined) return '—';
  if (ms < 1000) return ms + 'ms';
  const s = ms / 1000;
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60);
  const rem = Math.round(s - m * 60);
  return m + 'm ' + rem + 's';
}

function elapsed(state: RunState): number {
  const end = state.finishedAt ?? Date.now();
  return end - state.startedAt;
}

function totalTokens(state: RunState): { input: number; output: number } {
  let input = 0;
  let output = 0;
  for (const t of state.tasks) {
    input += t.inputTokens ?? 0;
    output += t.outputTokens ?? 0;
  }
  return { input, output };
}

function taskElementId(taskId: string): string {
  return 'task-card-' + taskId;
}

function getScrollY(): number {
  if (typeof window === 'undefined') return 0;
  return Math.max(window.scrollY ?? 0, 0);
}

function saveScrollY(): void {
  if (typeof window === 'undefined') return;
  try {
    window.sessionStorage.setItem(SCROLL_STORAGE_KEY, String(getScrollY()));
  } catch {
    // ignore storage failures
  }
}

function restoreScrollY(): void {
  if (typeof window === 'undefined') return;
  let target = 0;
  try {
    const raw = window.sessionStorage.getItem(SCROLL_STORAGE_KEY);
    if (!raw) return;
    const parsed = Number(raw);
    if (!Number.isFinite(parsed) || parsed <= 0) return;
    target = Math.floor(parsed);
  } catch {
    return;
  }

  // Retry because hot-reload can run before content height has settled.
  let attempts = 0;
  const maxAttempts = 8;
  const tick = (): void => {
    attempts += 1;
    const scrollHeight = Math.max(
      document.documentElement?.scrollHeight ?? 0,
      document.body?.scrollHeight ?? 0,
    );
    const maxY = Math.max(scrollHeight - window.innerHeight, 0);
    if (maxY <= 0) {
      if (attempts < maxAttempts) window.requestAnimationFrame(tick);
      return;
    }
    const desiredY = Math.min(target, maxY);
    window.scrollTo({ top: desiredY, behavior: 'auto' });
    if (attempts < maxAttempts && Math.abs(getScrollY() - desiredY) > 2) {
      window.requestAnimationFrame(tick);
    }
  };
  window.requestAnimationFrame(tick);
}

function DAGGraph({
  state,
  onNodeClick,
}: {
  state: RunState;
  onNodeClick?: (taskId: string) => void;
}): JSX.Element {
  const theme = useHostTheme();
  const layout = computeDAGLayout({
    nodes: state.tasks.map((t) => ({ id: t.id })),
    edges: state.tasks.flatMap((t) =>
      t.depends_on.map((d) => ({ from: d, to: t.id })),
    ),
    direction: 'vertical',
    nodeWidth: NODE_W,
    nodeHeight: NODE_H,
    rankGap: 72,
    nodeGap: 40,
    padding: 24,
  });

  const byId = new Map(state.tasks.map((t) => [t.id, t]));

  function nodeFill(status: TaskStatus): string {
    switch (status) {
      case 'PENDING':
        return theme.fill.tertiary;
      case 'RUNNING':
        return theme.fill.secondary;
      case 'FINISHED':
        return theme.fill.secondary;
      case 'ERROR':
        return theme.fill.secondary;
    }
  }

  function nodeStroke(status: TaskStatus): string {
    switch (status) {
      case 'PENDING':
        return theme.stroke.tertiary;
      case 'RUNNING':
        return theme.accent.primary;
      case 'FINISHED':
        return COMPLETED_DOT_COLOR;
      case 'ERROR':
        return theme.stroke.primary;
    }
  }

  function statusGlyph(status: TaskStatus): string {
    switch (status) {
      case 'PENDING':
        return '○';
      case 'RUNNING':
        return '◐';
      case 'FINISHED':
        return '●';
      case 'ERROR':
        return '×';
    }
  }

  function statusGlyphColor(status: TaskStatus): string {
    switch (status) {
      case 'PENDING':
        return theme.text.tertiary;
      case 'RUNNING':
        return theme.accent.primary;
      case 'FINISHED':
        return COMPLETED_DOT_COLOR;
      case 'ERROR':
        return theme.text.primary;
    }
  }

  return (
    <svg
      width={layout.width}
      height={layout.height}
      style={{ display: 'block', maxWidth: '100%' }}
    >
      <defs>
        <marker
          id="dag-arrow"
          viewBox="0 0 10 10"
          refX="9"
          refY="5"
          markerWidth="6"
          markerHeight="6"
          orient="auto-start-reverse"
        >
          <path d="M 0 0 L 10 5 L 0 10 z" fill={theme.stroke.secondary} />
        </marker>
      </defs>
      {layout.edges.map((e, i) => (
        <line
          key={'e' + i}
          x1={e.sourceX}
          y1={e.sourceY}
          x2={e.targetX}
          y2={e.targetY}
          stroke={theme.stroke.secondary}
          strokeWidth={1.25}
          strokeDasharray={e.isBackEdge ? '4 3' : undefined}
          markerEnd="url(#dag-arrow)"
        />
      ))}
      {layout.nodes.map((n) => {
        const t = byId.get(n.id);
        if (!t) return null;
        return (
          <g
            key={n.id}
            transform={'translate(' + n.x + ',' + n.y + ')'}
            onClick={() => onNodeClick?.(n.id)}
            style={{ cursor: onNodeClick ? 'pointer' : 'default' }}
          >
            <rect
              width={NODE_W}
              height={NODE_H}
              rx={8}
              ry={8}
              fill={nodeFill(t.status)}
              stroke={nodeStroke(t.status)}
              strokeWidth={t.status === 'RUNNING' ? 2 : 1}
            />
            <text
              x={12}
              y={22}
              fontSize={11}
              fontFamily="ui-monospace, SFMono-Regular, Menlo, monospace"
              fill={statusGlyphColor(t.status)}
            >
              {statusGlyph(t.status)}
            </text>
            <text
              x={28}
              y={22}
              fontSize={13}
              fontWeight={600}
              fill={theme.text.primary}
            >
              {t.id.length > 22 ? t.id.slice(0, 21) + '…' : t.id}
            </text>
            <text
              x={12}
              y={42}
              fontSize={10.5}
              fill={theme.text.secondary}
            >
              {t.complexity} · {t.model}
            </text>
            <text
              x={12}
              y={58}
              fontSize={10.5}
              fill={theme.text.tertiary}
            >
              {t.status === 'FINISHED' || t.status === 'ERROR'
                ? formatDuration(t.durationMs)
                : t.status === 'RUNNING'
                  ? 'running…'
                  : 'pending'}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function TaskList({
  state,
  forcedOpenVersionByTaskId,
}: {
  state: RunState;
  forcedOpenVersionByTaskId: Record<string, number>;
}): JSX.Element {
  const theme = useHostTheme();
  return (
    <Stack gap={10}>
      {state.tasks.map((t) => {
        const trailing = (
          <Row gap={6} align="center">
            <Pill tone={complexityTone(t.complexity)} size="sm">
              {t.complexity}
            </Pill>
            <Pill tone={pillToneFor(t.status)} active={t.status !== 'PENDING'} size="sm">
              {t.status}
            </Pill>
          </Row>
        );
        return (
          <div key={t.id} id={taskElementId(t.id)}>
            <Card
              key={t.id + ':' + String(forcedOpenVersionByTaskId[t.id] ?? 0)}
              collapsible
              defaultOpen={t.status !== 'PENDING' || (forcedOpenVersionByTaskId[t.id] ?? 0) > 0}
            >
            <CardHeader trailing={trailing}>{t.id}</CardHeader>
            <CardBody>
              <Stack gap={8}>
                <Text tone="secondary" size="small">
                  Model {t.model}
                  {t.depends_on.length > 0 ? ' · depends on ' + t.depends_on.join(', ') : ''}
                  {t.durationMs !== undefined ? ' · ' + formatDuration(t.durationMs) : ''}
                  {t.inputTokens !== undefined || t.outputTokens !== undefined
                    ? ' · ' + (t.inputTokens ?? 0) + ' in / ' + (t.outputTokens ?? 0) + ' out tokens'
                    : ''}
                </Text>
                <Text size="small">
                  <Text as="span" weight="semibold">Prompt: </Text>
                  {t.subtask_prompt}
                </Text>
                {t.resultText ? (
                  <Stack gap={4}>
                    <Text size="small" weight="semibold">
                      {t.status === 'RUNNING' ? 'Streaming output' : 'Result'}
                    </Text>
                    <pre
                      style={{
                        margin: 0,
                        padding: 10,
                        borderRadius: 6,
                        background: theme.bg.elevated,
                        border: '1px solid ' + theme.stroke.tertiary,
                        color: theme.text.secondary,
                        fontSize: 12,
                        lineHeight: 1.45,
                        fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                        whiteSpace: 'pre-wrap',
                        wordBreak: 'break-word',
                        maxHeight: 320,
                        overflow: 'auto',
                      }}
                    >
                      {t.resultText}
                      {t.status === 'RUNNING' ? '\u2588' : ''}
                    </pre>
                  </Stack>
                ) : t.status === 'RUNNING' ? (
                  <Text size="small" tone="tertiary" italic>
                    Waiting for first token…
                  </Text>
                ) : null}
                {t.errorMessage ? (
                  <Stack gap={4}>
                    <Text size="small" weight="semibold">Error</Text>
                    <Text size="small" tone="secondary">{t.errorMessage}</Text>
                  </Stack>
                ) : null}
              </Stack>
            </CardBody>
            </Card>
          </div>
        );
      })}
    </Stack>
  );
}

export default function DagRun(): JSX.Element {
  const [forcedOpenVersionByTaskId, setForcedOpenVersionByTaskId] = useState<Record<string, number>>({});
  const taskIds = useMemo(() => new Set(STATE.tasks.map((t) => t.id)), []);

  useEffect(() => {
    restoreScrollY();
    const onScroll = (): void => saveScrollY();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => {
      saveScrollY();
      window.removeEventListener('scroll', onScroll);
    };
  }, []);

  const handleNodeClick = (taskId: string): void => {
    if (!taskIds.has(taskId)) return;

    setForcedOpenVersionByTaskId((prev) => ({
      ...prev,
      [taskId]: (prev[taskId] ?? 0) + 1,
    }));

    const targetId = taskElementId(taskId);
    const scrollToTask = (): void => {
      const el = document.getElementById(targetId);
      if (!el) return;
      el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };

    // Wait one frame so the forced-open remount lands before we scroll.
    window.requestAnimationFrame(scrollToTask);
  };

  const counts = STATE.tasks.reduce(
    (acc, t) => {
      acc.total += 1;
      acc[t.status.toLowerCase() as 'pending' | 'running' | 'finished' | 'error'] += 1;
      return acc;
    },
    { total: 0, pending: 0, running: 0, finished: 0, error: 0 },
  );
  const tokens = totalTokens(STATE);
  const isFinal = STATE.finishedAt !== undefined;
  const statusLabel =
    STATE.runOutcome === 'INTERRUPTED'
      ? 'INTERRUPTED'
      : STATE.runOutcome === 'FAILED'
        ? 'FAILED'
        : isFinal
          ? 'COMPLETE'
          : 'RUNNING';
  const statusTone =
    STATE.runOutcome === 'INTERRUPTED' || STATE.runOutcome === 'FAILED'
      ? 'danger'
      : isFinal
        ? 'success'
        : 'info';

  return (
    <Stack gap={20}>
      <Stack gap={6}>
        <H1>{STATE.title}</H1>
        <Row gap={10} align="center">
          <Pill tone={statusTone} active size="sm">
            {statusLabel}
          </Pill>
          <Text tone="secondary" size="small">
            {counts.total} tasks · elapsed {formatDuration(elapsed(STATE))}
            {tokens.input + tokens.output > 0
              ? ' · ' + tokens.input + ' in / ' + tokens.output + ' out tokens'
              : ''}
          </Text>
        </Row>
        {STATE.runMessage ? (
          <Text tone="secondary" size="small">
            {STATE.runMessage}
          </Text>
        ) : null}
      </Stack>

      <Grid columns={5} gap={12}>
        <Stat value={String(counts.total)} label="Total" />
        <Stat value={String(counts.pending)} label="Pending" />
        <Stat value={String(counts.running)} label="Running" tone={counts.running > 0 ? 'info' : undefined} />
        <Stat value={String(counts.finished)} label="Finished" tone={counts.finished > 0 ? 'success' : undefined} />
        <Stat value={String(counts.error)} label="Errored" tone={counts.error > 0 ? 'danger' : undefined} />
      </Grid>

      <Divider />

      <Stack gap={12}>
        <H2>Graph</H2>
        <DAGGraph state={STATE} onNodeClick={handleNodeClick} />
      </Stack>

      <Divider />

      <Stack gap={12}>
        <H2>Tasks</H2>
        <TaskList state={STATE} forcedOpenVersionByTaskId={forcedOpenVersionByTaskId} />
      </Stack>
    </Stack>
  );
}
