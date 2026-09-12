// Missions that have opted into the provisional TX offset-sweep search —
// mirrors `_OFFSET_SWEEP_MISSIONS` in the backend's TrackingService. The
// backend is the actual source of truth/enforcement (a toggle attempt for
// any other mission is refused there); this only decides whether the UI
// bothers to offer the toggle/column/settings at all.
const OFFSET_SWEEP_MISSIONS = new Set(['maveric'])

export function isOffsetSweepMission(missionId: string | undefined | null): boolean {
  return !!missionId && OFFSET_SWEEP_MISSIONS.has(missionId)
}
