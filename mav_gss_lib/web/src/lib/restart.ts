const INITIAL_GRACE_MS = 500
const POLL_INTERVAL_MS = 400
const POLL_TIMEOUT_MS = 1500
const MAX_WAIT_MS = 60_000

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/**
 * Poll `/api/status` until the restarted server reports the target mission,
 * then hard-reload the page.
 *
 * Readiness is `mission === targetId`, not just a 200: the old process keeps
 * answering for a short window before `os.execv` fires (and radio teardown
 * can take several seconds), so a plain liveness probe would reload straight
 * back into the old mission.
 */
export async function waitForMissionThenReload(targetId: string): Promise<void> {
  await sleep(INITIAL_GRACE_MS)
  const deadline = Date.now() + MAX_WAIT_MS
  while (Date.now() < deadline) {
    const ctrl = new AbortController()
    const timer = setTimeout(() => ctrl.abort(), POLL_TIMEOUT_MS)
    try {
      const resp = await fetch('/api/status', { cache: 'no-store', signal: ctrl.signal })
      if (resp.ok) {
        const data = (await resp.json()) as { mission?: string }
        if (data.mission === targetId) break
      }
    } catch {
      // server mid-restart — keep polling
    } finally {
      clearTimeout(timer)
    }
    await sleep(POLL_INTERVAL_MS)
  }
  window.location.reload()
}
