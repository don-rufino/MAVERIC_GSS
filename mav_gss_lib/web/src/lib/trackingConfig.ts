import type { GssConfig, MissionConfig, PlatformConfig, PlatformTrackingConfig } from '@/lib/types'

export const DOPPLER_CONFIG_KEY = 'doppler'
export const TRACKING_CONFIG_KEY = 'tracking'

// Neutral sample TLE. Mirrors `SAMPLE_TLE_LINE1/2` in
// `mav_gss_lib/platform/tracking/models.py` — only used as a fallback
// when the server hasn't supplied a real TLE via /api/config. Mission
// values (e.g. MAVERIC's actual orbital elements) flow through the
// backend config; this constant is mission-agnostic.
export const SAMPLE_TLE = [
  '1 25544U 98067A   26001.50000000  .00000000  00000-0  00000-0 0  9990',
  '2 25544  51.6400   0.0000 0000000   0.0000   0.0000 15.50000000000007',
].join('\n')

export interface TrackingConfig {
  selectedStationId: string
  stations: TrackingStation[]
  stationName: string
  stationLatDeg: string
  stationLonDeg: string
  stationAltM: string
  minElevationDeg: string
  downlinkHz: string
  uplinkHz: string
  tleSource: string
  tleText: string
  showDayNight: boolean
}

export interface TrackingStation {
  id: string
  name: string
  latDeg: string
  lonDeg: string
  altM: string
  minElevationDeg: string
}

// Neutral reference station — mission-agnostic UI fallback used only
// before /api/config has loaded. Backend ships matching defaults
// (`default_station()` in mav_gss_lib/platform/tracking/config.py);
// MAVERIC's actual station (USC / Southern California) is seeded by
// the mission's tracking_defaults.py and arrives via the config feed.
export const DEFAULT_TRACKING_STATION: TrackingStation = {
  id: 'ref',
  name: 'Reference Station',
  latDeg: '0',
  lonDeg: '0',
  altM: '0',
  minElevationDeg: '5',
}

export const DEFAULT_TRACKING_CONFIG: TrackingConfig = {
  selectedStationId: DEFAULT_TRACKING_STATION.id,
  stations: [DEFAULT_TRACKING_STATION],
  stationName: DEFAULT_TRACKING_STATION.name,
  stationLatDeg: DEFAULT_TRACKING_STATION.latDeg,
  stationLonDeg: DEFAULT_TRACKING_STATION.lonDeg,
  stationAltM: DEFAULT_TRACKING_STATION.altM,
  minElevationDeg: DEFAULT_TRACKING_STATION.minElevationDeg,
  downlinkHz: '437575000',
  uplinkHz: '437575000',
  tleSource: 'platform sample TLE',
  tleText: SAMPLE_TLE,
  showDayNight: true,
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function stringField(source: Record<string, unknown>, key: keyof TrackingConfig): string {
  const value = source[key]
  const fallback = DEFAULT_TRACKING_CONFIG[key]
  if (typeof value === 'string') return value
  if (typeof value === 'number' && Number.isFinite(value)) return String(value)
  return typeof fallback === 'string' ? fallback : ''
}

function stationFromRecord(value: unknown): TrackingStation | null {
  if (!isRecord(value)) return null
  const id = typeof value.id === 'string' && value.id.trim() ? value.id.trim() : null
  const name = typeof value.name === 'string' && value.name.trim() ? value.name.trim() : null
  if (!id || !name) return null
  return {
    id,
    name,
    latDeg: typeof value.latDeg === 'number' ? String(value.latDeg) : typeof value.latDeg === 'string' ? value.latDeg : DEFAULT_TRACKING_STATION.latDeg,
    lonDeg: typeof value.lonDeg === 'number' ? String(value.lonDeg) : typeof value.lonDeg === 'string' ? value.lonDeg : DEFAULT_TRACKING_STATION.lonDeg,
    altM: typeof value.altM === 'number' ? String(value.altM) : typeof value.altM === 'string' ? value.altM : DEFAULT_TRACKING_STATION.altM,
    minElevationDeg: typeof value.minElevationDeg === 'number'
      ? String(value.minElevationDeg)
      : typeof value.minElevationDeg === 'string'
        ? value.minElevationDeg
        : DEFAULT_TRACKING_STATION.minElevationDeg,
  }
}

function stationFromPlatformRecord(value: unknown): TrackingStation | null {
  if (!isRecord(value)) return null
  const id = typeof value.id === 'string' && value.id.trim() ? value.id.trim() : null
  const name = typeof value.name === 'string' && value.name.trim() ? value.name.trim() : null
  if (!id || !name) return null
  return {
    id,
    name,
    latDeg: typeof value.lat_deg === 'number' ? String(value.lat_deg) : typeof value.lat_deg === 'string' ? value.lat_deg : DEFAULT_TRACKING_STATION.latDeg,
    lonDeg: typeof value.lon_deg === 'number' ? String(value.lon_deg) : typeof value.lon_deg === 'string' ? value.lon_deg : DEFAULT_TRACKING_STATION.lonDeg,
    altM: typeof value.alt_m === 'number' ? String(value.alt_m) : typeof value.alt_m === 'string' ? value.alt_m : DEFAULT_TRACKING_STATION.altM,
    minElevationDeg: typeof value.min_elevation_deg === 'number'
      ? String(value.min_elevation_deg)
      : typeof value.min_elevation_deg === 'string'
        ? value.min_elevation_deg
        : DEFAULT_TRACKING_STATION.minElevationDeg,
  }
}

function legacyStationFromConfig(source: Record<string, unknown>): TrackingStation {
  return {
    id: DEFAULT_TRACKING_STATION.id,
    name: stringField(source, 'stationName') || DEFAULT_TRACKING_STATION.name,
    latDeg: stringField(source, 'stationLatDeg') || DEFAULT_TRACKING_STATION.latDeg,
    lonDeg: stringField(source, 'stationLonDeg') || DEFAULT_TRACKING_STATION.lonDeg,
    altM: stringField(source, 'stationAltM') || DEFAULT_TRACKING_STATION.altM,
    minElevationDeg: stringField(source, 'minElevationDeg') || DEFAULT_TRACKING_STATION.minElevationDeg,
  }
}

export function normalizeTrackingConfig(value: unknown): TrackingConfig {
  if (!isRecord(value)) return DEFAULT_TRACKING_CONFIG
  const legacyStation = legacyStationFromConfig(value)
  const stations = Array.isArray(value.stations)
    ? value.stations.map(stationFromRecord).filter((station): station is TrackingStation => Boolean(station))
    : []
  const normalizedStations = stations.length > 0 ? stations : [legacyStation]
  const selectedStationId = typeof value.selectedStationId === 'string'
    && normalizedStations.some(station => station.id === value.selectedStationId)
    ? value.selectedStationId
    : normalizedStations[0].id
  const activeStation = normalizedStations.find(station => station.id === selectedStationId) ?? normalizedStations[0]
  return {
    selectedStationId,
    stations: normalizedStations,
    stationName: activeStation.name,
    stationLatDeg: activeStation.latDeg,
    stationLonDeg: activeStation.lonDeg,
    stationAltM: activeStation.altM,
    minElevationDeg: activeStation.minElevationDeg,
    downlinkHz: stringField(value, 'downlinkHz'),
    uplinkHz: stringField(value, 'uplinkHz'),
    tleSource: stringField(value, 'tleSource'),
    tleText: stringField(value, 'tleText'),
    showDayNight: typeof value.showDayNight === 'boolean' ? value.showDayNight : DEFAULT_TRACKING_CONFIG.showDayNight,
  }
}

export function normalizePlatformTrackingConfig(value: unknown): TrackingConfig {
  if (!isRecord(value)) return DEFAULT_TRACKING_CONFIG
  const stations = Array.isArray(value.stations)
    ? value.stations.map(stationFromPlatformRecord).filter((station): station is TrackingStation => Boolean(station))
    : []
  const normalizedStations = stations.length > 0 ? stations : [DEFAULT_TRACKING_STATION]
  const selectedStationId = typeof value.selected_station_id === 'string'
    && normalizedStations.some(station => station.id === value.selected_station_id)
    ? value.selected_station_id
    : normalizedStations[0].id
  const activeStation = normalizedStations.find(station => station.id === selectedStationId) ?? normalizedStations[0]
  const tle = isRecord(value.tle) ? value.tle : {}
  const frequencies = isRecord(value.frequencies) ? value.frequencies : {}
  const display = isRecord(value.display) ? value.display : {}
  const line1 = typeof tle.line1 === 'string' ? tle.line1 : SAMPLE_TLE.split('\n')[0]
  const line2 = typeof tle.line2 === 'string' ? tle.line2 : SAMPLE_TLE.split('\n')[1]

  return {
    selectedStationId,
    stations: normalizedStations,
    stationName: activeStation.name,
    stationLatDeg: activeStation.latDeg,
    stationLonDeg: activeStation.lonDeg,
    stationAltM: activeStation.altM,
    minElevationDeg: activeStation.minElevationDeg,
    downlinkHz: typeof frequencies.rx_hz === 'number' ? String(frequencies.rx_hz) : DEFAULT_TRACKING_CONFIG.downlinkHz,
    uplinkHz: typeof frequencies.tx_hz === 'number' ? String(frequencies.tx_hz) : DEFAULT_TRACKING_CONFIG.uplinkHz,
    tleSource: typeof tle.source === 'string' ? tle.source : DEFAULT_TRACKING_CONFIG.tleSource,
    tleText: [line1, line2].join('\n'),
    showDayNight: typeof display.day_night_map === 'boolean' ? display.day_night_map : DEFAULT_TRACKING_CONFIG.showDayNight,
  }
}

export function getDopplerConfigFromMissionConfig(missionConfig: MissionConfig | null | undefined): TrackingConfig {
  return normalizeTrackingConfig(missionConfig?.[DOPPLER_CONFIG_KEY])
}

export function getTrackingConfigFromPlatformConfig(platformConfig: PlatformConfig | null | undefined): TrackingConfig {
  return normalizePlatformTrackingConfig(platformConfig?.[TRACKING_CONFIG_KEY])
}

export function getTrackingConfig(config: GssConfig | null | undefined): TrackingConfig {
  if (config?.platform?.tracking) return getTrackingConfigFromPlatformConfig(config.platform)
  return getDopplerConfigFromMissionConfig(config?.mission.config)
}

export function getDopplerConfig(config: GssConfig | null | undefined): TrackingConfig {
  return getTrackingConfig(config)
}

export function getActiveTrackingStation(config: TrackingConfig): TrackingStation {
  return config.stations.find(station => station.id === config.selectedStationId) ?? config.stations[0] ?? DEFAULT_TRACKING_STATION
}

export function updateActiveTrackingStation(config: TrackingConfig, patch: Partial<TrackingStation>): TrackingConfig {
  const active = getActiveTrackingStation(config)
  const updated = { ...active, ...patch }
  const stations = config.stations.some(station => station.id === active.id)
    ? config.stations.map(station => station.id === active.id ? updated : station)
    : [updated]
  return {
    ...config,
    selectedStationId: updated.id,
    stations,
    stationName: updated.name,
    stationLatDeg: updated.latDeg,
    stationLonDeg: updated.lonDeg,
    stationAltM: updated.altM,
    minElevationDeg: updated.minElevationDeg,
  }
}

export function selectTrackingStation(config: TrackingConfig, stationId: string): TrackingConfig {
  const station = config.stations.find(item => item.id === stationId)
  if (!station) return config
  return {
    ...config,
    selectedStationId: station.id,
    stationName: station.name,
    stationLatDeg: station.latDeg,
    stationLonDeg: station.lonDeg,
    stationAltM: station.altM,
    minElevationDeg: station.minElevationDeg,
  }
}

function numericString(value: string, fallback: number): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : fallback
}

export function trackingConfigToPlatformSection(config: TrackingConfig): PlatformTrackingConfig {
  const [line1, line2] = config.tleText.split(/\r?\n/).map(line => line.trim()).filter(Boolean)
  return {
    enabled: true,
    selected_station_id: config.selectedStationId,
    stations: config.stations.map(station => ({
      id: station.id,
      name: station.name,
      lat_deg: numericString(station.latDeg, DEFAULT_TRACKING_STATION.latDeg ? Number(DEFAULT_TRACKING_STATION.latDeg) : 34.0205),
      lon_deg: numericString(station.lonDeg, DEFAULT_TRACKING_STATION.lonDeg ? Number(DEFAULT_TRACKING_STATION.lonDeg) : -118.2856),
      alt_m: numericString(station.altM, DEFAULT_TRACKING_STATION.altM ? Number(DEFAULT_TRACKING_STATION.altM) : 70),
      min_elevation_deg: numericString(station.minElevationDeg, DEFAULT_TRACKING_STATION.minElevationDeg ? Number(DEFAULT_TRACKING_STATION.minElevationDeg) : 5),
    })),
    tle: {
      source: config.tleSource,
      // Spacecraft name is owned by the backend's tracking config —
      // operator sets it via gss.yml `platform.tracking.tle.name`,
      // and missions seed their own (e.g. MAVERIC's mission.py
      // calls `seed_tracking_defaults`). The frontend doesn't yet
      // surface a name editor, so the writeback uses a neutral
      // placeholder that the backend's existing `tle.name` will
      // override on the next round-trip.
      name: 'Spacecraft',
      line1: line1 || SAMPLE_TLE.split('\n')[0],
      line2: line2 || SAMPLE_TLE.split('\n')[1],
    },
    frequencies: {
      rx_hz: numericString(config.downlinkHz, Number(DEFAULT_TRACKING_CONFIG.downlinkHz)),
      tx_hz: numericString(config.uplinkHz, Number(DEFAULT_TRACKING_CONFIG.uplinkHz)),
    },
    display: {
      day_night_map: config.showDayNight,
    },
  }
}
