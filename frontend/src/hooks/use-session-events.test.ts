// @vitest-environment jsdom
import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { sessionApi } from '@/lib/session-api'
import { liveText, type TraceEvent } from '@/state'
import { useSessionEvents } from './use-session-events'

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = []
  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  close = vi.fn()
  constructor(public url: string) {
    super()
    FakeEventSource.instances.push(this)
  }
  push(event: TraceEvent) {
    this.dispatchEvent(new MessageEvent('trace', { data: JSON.stringify(event) }))
  }
}
const event = (seq: number, type: string, content = ''): TraceEvent => ({
  schema_version: 1,
  event_id: `e${seq}`,
  session_id: 's1',
  seq,
  run_id: 'r1',
  call_id: 'c1',
  parent_call_id: null,
  timestamp: '',
  type,
  payload: { content },
})
const history = [event(1, 'run.started'), event(2, 'model.request')]
const refresh = vi.fn(async () => {})
const report = vi.fn()
let frames: Map<number, FrameRequestCallback>
beforeEach(() => {
  vi.useFakeTimers()
  frames = new Map()
  let frameId = 0
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.stubGlobal(
    'requestAnimationFrame',
    vi.fn((callback: FrameRequestCallback) => {
      frames.set(++frameId, callback)
      return frameId
    }),
  )
  vi.stubGlobal(
    'cancelAnimationFrame',
    vi.fn((id: number) => frames.delete(id)),
  )
  vi.spyOn(sessionApi, 'getEvents').mockResolvedValue(history)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})
async function mount() {
  const hook = renderHook(({ id }) => useSessionEvents(id, 0, refresh, report), {
    initialProps: { id: 's1' },
  })
  await act(async () => {})
  return hook
}
it('coalesces deltas and wire frames into one animation frame publication', async () => {
  const { result } = await mount()
  const before = result.current.events
  act(() => {
    const stream = FakeEventSource.instances[0]
    stream.push(event(3, 'model.delta', '你'))
    stream.push(event(4, 'model.chunk'))
    stream.push(event(5, 'model.delta', '好'))
  })
  expect(result.current.events).toBe(before)
  expect(frames.size).toBe(1)
  act(() => {
    ;[...frames.values()][0](16)
  })
  expect(liveText(result.current.events)).toBe('你好')
  expect(result.current.events).toHaveLength(5)
  const after = result.current.events
  act(() => vi.advanceTimersByTime(32))
  expect(result.current.events).toBe(after)
})
it('flushes pending text before terminal events', async () => {
  const { result } = await mount()
  act(() => {
    const stream = FakeEventSource.instances[0]
    stream.push(event(3, 'model.delta', '部分'))
    stream.push(event(4, 'run.interrupted'))
  })
  expect(result.current.events.map((item) => item.seq)).toEqual([1, 2, 3, 4])
  expect(result.current.status).toBe('interrupted')
  expect(liveText(result.current.events)).toBe('部分')
  expect(frames.size).toBe(0)
})
it('uses the timeout when animation frames are suspended', async () => {
  const { result } = await mount()
  act(() => FakeEventSource.instances[0].push(event(3, 'model.delta', '后台')))
  act(() => vi.advanceTimersByTime(32))
  expect(liveText(result.current.events)).toBe('后台')
  expect(frames.size).toBe(0)
})
it('discards pending events on session switching and deletion', async () => {
  const { result, rerender } = await mount()
  const old = FakeEventSource.instances[0]
  act(() => old.push(event(3, 'model.delta', '旧会话')))
  await act(async () => rerender({ id: 's2' }))
  expect(old.close).toHaveBeenCalled()
  act(() => vi.advanceTimersByTime(32))
  expect(liveText(result.current.events)).toBe('')
  act(() => {
    const current = FakeEventSource.instances[1]
    current.push(event(3, 'model.delta', '已删除'))
    current.dispatchEvent(new Event('session-deleted'))
  })
  act(() => vi.advanceTimersByTime(32))
  expect(result.current.events).toEqual([])
  expect(frames.size).toBe(0)
})
