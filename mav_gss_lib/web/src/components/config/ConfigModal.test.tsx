import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { ConfigModal } from './ConfigModal'

const CONFIG = {
  platform: {
    rx: { zmq_addr: 'tcp://127.0.0.1:52001', tx_blackout_ms: 0 },
    tx: { zmq_addr: 'tcp://127.0.0.1:52002', delay_ms: 200, frequency: '437.575 MHz', verifiers_enabled: true },
    tracking: {
      tle: { source: 'CelesTrak', name: 'X', line1: '1 99999U', line2: '2 99999' },
      tle_fetch: { identifier: '99999', auto_refresh: false, refresh_interval_hours: 12 },
      frequencies: { rx_hz: 437575000, tx_hz: 437575000 },
    },
  },
  mission: { id: 'maveric', name: 'MAVERIC', config: { csp: { source: 1, dest: 5 }, imaging: { thumb_prefix: 'thumb_' } } },
}
const FAMILY_CONFIG = {
  platform: {
    ...CONFIG.platform,
    rx: { zmq_addr: 'tcp://127.0.0.1:52001', tx_blackout_ms: 0, frequency: '435.400 MHz' },
    tracking: {
      ...CONFIG.platform.tracking,
      tle_fetch: { identifier: '64535', auto_refresh: false, refresh_interval_hours: 12 },
      frequencies: { rx_hz: 435400000, tx_hz: 435400000 },
    },
  },
  mission: {
    id: 'roads', name: 'ROADS',
    config: {
      mission_name: 'ROADS',
      target_birds: [
        { id: 'roads1', label: 'ROADS 1', norad: 64535, rx_frequency: '435.400 MHz' },
        { id: 'roads2', label: 'ROADS 2', norad: 64549, rx_frequency: '435.400 MHz' },
      ],
    },
  },
}
const STATUS = { version: '1.2.3', schema_path: '/x/maveric.yml', schema_count: 42, log_dir: '/var/log/gss', session_log_json: '/var/log/gss/json/session_x.jsonl' }

let configBody: unknown = CONFIG

beforeEach(() => {
  configBody = CONFIG
  global.fetch = vi.fn((url: RequestInfo | URL) => {
    const u = String(url)
    const body = u.includes('/api/tracking/tle/status')
      ? { ok: false, spacetrack: { identity_set: true, password_set: false } }
      : u.includes('/api/status') ? STATUS : configBody
    return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response)
  }) as unknown as typeof fetch
})

async function openModal() {
  render(<ConfigModal open={true} onClose={() => {}} />)
  await screen.findByText('Save & Close')
}

function rail() {
  return screen.getByRole('navigation', { name: 'Settings categories' })
}

describe('ConfigModal', () => {
  it('lists all four rail categories', async () => {
    await openModal()
    const r = rail()
    expect(within(r).getByRole('button', { name: /Mission/ })).toBeTruthy()
    expect(within(r).getByRole('button', { name: /Radio \/ RF/ })).toBeTruthy()
    expect(within(r).getByRole('button', { name: /Tracking/ })).toBeTruthy()
    expect(within(r).getByRole('button', { name: /About/ })).toBeTruthy()
  })

  it('Radio/RF pane keeps every transport + timing + capture setting', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByText('Radio / RF'))
    expect(screen.getByText('RX frequency')).toBeTruthy()
    expect(screen.getByText('TX frequency')).toBeTruthy()
    expect(screen.getByText('RX ZMQ address')).toBeTruthy()
    expect(screen.getByText('TX ZMQ address')).toBeTruthy()
    expect(screen.getByText('TX delay')).toBeTruthy()
    expect(screen.getByText('TX → RX blackout')).toBeTruthy()
    expect(screen.getByText('Command verifiers')).toBeTruthy()
    expect(screen.getByText(/Experimental satellite-hunting only.*Keep enabled for normal operations/)).toBeTruthy()
    const verifierSwitch = screen.getByRole('switch', { name: 'Command verifiers' })
    expect(verifierSwitch.getAttribute('aria-checked')).toBe('true')
    const descriptionId = verifierSwitch.getAttribute('aria-describedby')
    expect(descriptionId).toBeTruthy()
    expect(document.getElementById(descriptionId!)?.textContent).toMatch(/Experimental satellite-hunting only.*cancels active verification/)
    expect(screen.getByText('RX gain')).toBeTruthy()
    expect(screen.getByText('IQ recording')).toBeTruthy()
    expect(screen.getByText('Raw 1 Msps capture')).toBeTruthy()
    expect(screen.getAllByRole('switch').length).toBe(3)
  })

  it('saves only the command-verifier toggle diff', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByText('Radio / RF'))
    fireEvent.click(screen.getByRole('switch', { name: 'Command verifiers' }))
    fireEvent.click(screen.getByText('Save & Close'))

    await waitFor(() => {
      const putCall = vi.mocked(global.fetch).mock.calls.find(([url, init]) =>
        String(url) === '/api/config' && init?.method === 'PUT')
      expect(putCall).toBeTruthy()
      expect(JSON.parse(String(putCall?.[1]?.body))).toEqual({
        platform: { tx: { verifiers_enabled: false } },
      })
    })
  })

  it('Tracking pane keeps TLE + auto-fetch settings and renders a switch', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByText('Tracking'))
    expect(screen.getByText('TLE source')).toBeTruthy()
    expect(screen.getByText('Two-line elements (TLE)')).toBeTruthy()
    expect(screen.getByText('Catalog identifier')).toBeTruthy()
    expect(screen.getByText('Auto-refresh')).toBeTruthy()
    expect(screen.getByText('Refresh interval')).toBeTruthy()
    expect(screen.getAllByRole('switch').length).toBe(1)
  })

  it('Mission pane renders dynamic mission config', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByRole('button', { name: /Mission/ }))
    expect(screen.getByText('Csp')).toBeTruthy()
    expect(screen.getByText('Imaging')).toBeTruthy()
    expect(screen.getByText('Thumb Prefix')).toBeTruthy()
  })

  it('family missions get a Target-satellite select that fills identifier + RX freq', async () => {
    configBody = FAMILY_CONFIG
    await openModal()
    fireEvent.click(within(rail()).getByRole('button', { name: /Mission/ }))
    expect(screen.getByText('Target satellite')).toBeTruthy()
    const select = screen.getAllByDisplayValue('ROADS 1 · 435.400 MHz').find((el) => el.tagName === 'SELECT')
    expect(select).toBeTruthy()
    expect(screen.getByText(/Same frequency as saved/)).toBeTruthy()
    fireEvent.change(select!, { target: { value: 'roads2' } })
    fireEvent.click(within(rail()).getByText('Tracking'))
    expect(screen.getByDisplayValue('64549')).toBeTruthy()
  })

  it('single-bird missions render no Target-satellite select', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByRole('button', { name: /Mission/ }))
    expect(screen.queryByText('Target satellite')).toBeNull()
  })

  it('About pane shows read-only session info', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByText('About'))
    expect(screen.getByText('Version')).toBeTruthy()
    expect(screen.getByText('1.2.3')).toBeTruthy()
    expect(screen.getByText('Commands')).toBeTruthy()
    expect(screen.getByText('Schema')).toBeTruthy()
    expect(screen.getByText('Log dir')).toBeTruthy()
    expect(screen.getByText('Session data')).toBeTruthy()
  })

  it('search filters fields across panes', async () => {
    await openModal()
    fireEvent.change(screen.getByPlaceholderText('Search settings'), { target: { value: 'blackout' } })
    expect(screen.getByText('TX → RX blackout')).toBeTruthy()
    expect(screen.queryByText('RX frequency')).toBeNull()
    expect(screen.queryByText('Auto-refresh')).toBeNull()
  })

  it('shows an empty-state when the search matches nothing', async () => {
    await openModal()
    fireEvent.change(screen.getByPlaceholderText('Search settings'), { target: { value: 'zzzzznomatch' } })
    expect(screen.getByText(/No settings match/)).toBeTruthy()
  })

  it('shows the provider dropdown and reveals the env-status panel for Space-Track', async () => {
    await openModal()
    fireEvent.click(within(rail()).getByText('Tracking'))
    const select = screen.getAllByDisplayValue('CelesTrak').find((el) => el.tagName === 'SELECT')
    expect(select).toBeTruthy()
    expect(screen.queryByText('SPACETRACK_IDENTITY')).toBeNull()
    fireEvent.change(select!, { target: { value: 'spacetrack' } })
    expect(screen.getByText('SPACETRACK_IDENTITY')).toBeTruthy()
    expect(screen.getByText('SPACETRACK_PASSWORD')).toBeTruthy()
  })
})
