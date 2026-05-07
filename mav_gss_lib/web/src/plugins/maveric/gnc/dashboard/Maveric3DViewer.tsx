import { useEffect, useRef } from 'react'
import * as THREE from 'three'
import { STLLoader } from 'three/examples/jsm/loaders/STLLoader.js'
import maveric3DModelUrl from '@/assets/maveric.stl?url'
import { useTabActive } from '@/state/TabActiveContext'

interface Maveric3DViewerProps {
  /** Scalar-first attitude quaternion [q0, q1, q2, q3]. null → identity. */
  q: number[] | null
}

export function Maveric3DViewer({ q }: Maveric3DViewerProps) {
  const mountRef = useRef<HTMLDivElement>(null)
  const bodyGroupRef = useRef<THREE.Group | null>(null)
  const rendererRef = useRef<THREE.WebGLRenderer | null>(null)
  const sceneRef = useRef<THREE.Scene | null>(null)
  const cameraRef = useRef<THREE.PerspectiveCamera | null>(null)
  const tabActive = useTabActive()

  useEffect(() => {
    const mount = mountRef.current
    if (!mount) return
    let cancelled = false
    const disposables: Array<{ dispose: () => void }> = []

    ;(async () => {
      await document.fonts.ready
      if (cancelled) return

      const W = mount.clientWidth || 1
      const H = mount.clientHeight || 1

      const scene = new THREE.Scene()
      scene.background = null
      const camera = new THREE.PerspectiveCamera(42, W / H, 0.1, 100)
      camera.position.set(2.4, 1.8, 2.4)
      camera.lookAt(0, 0, 0)
      const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
      renderer.setPixelRatio(window.devicePixelRatio)
      renderer.setSize(W, H)
      renderer.setClearColor(0x000000, 0)
      mount.appendChild(renderer.domElement)

      scene.add(new THREE.AmbientLight(0xffffff, 0.55))
      const keyLight = new THREE.DirectionalLight(0xffffff, 0.9)
      keyLight.position.set(3, 4, 2)
      scene.add(keyLight)
      const fillLight = new THREE.DirectionalLight(0x5AA8F0, 0.35)
      fillLight.position.set(-3, -1, -2)
      scene.add(fillLight)

      const triad = new THREE.Group()
      const addAxis = (dir: THREE.Vector3, color: number) => {
        const mat = new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.85 })
        const geo = new THREE.BufferGeometry().setFromPoints([
          new THREE.Vector3(0, 0, 0),
          new THREE.Vector3(dir.x * 1.3, dir.y * 1.3, dir.z * 1.3),
        ])
        disposables.push(geo, mat)
        triad.add(new THREE.Line(geo, mat))
      }
      addAxis(new THREE.Vector3(1, 0, 0), 0xFF3838)
      addAxis(new THREE.Vector3(0, 1, 0), 0x3CC98E)
      addAxis(new THREE.Vector3(0, 0, 1), 0x5AA8F0)
      triad.scale.setScalar(0.85)
      scene.add(triad)

      const makeLabel = (text: string, colorHex: string): THREE.Sprite => {
        const canvas = document.createElement('canvas')
        canvas.width = 256
        canvas.height = 128
        const ctx = canvas.getContext('2d')!
        ctx.font = 'bold 96px "JetBrains Mono", ui-monospace, monospace'
        ctx.fillStyle = colorHex
        ctx.textAlign = 'center'
        ctx.textBaseline = 'middle'
        ctx.fillText(text, canvas.width / 2, canvas.height / 2)
        const texture = new THREE.CanvasTexture(canvas)
        texture.minFilter = THREE.LinearFilter
        texture.magFilter = THREE.LinearFilter
        const mat = new THREE.SpriteMaterial({
          map: texture,
          transparent: true,
          depthTest: false,
          depthWrite: false,
        })
        const sprite = new THREE.Sprite(mat)
        sprite.scale.set(0.55, 0.275, 1)
        sprite.renderOrder = 999
        disposables.push(texture, mat)
        return sprite
      }
      for (const { dir, text, hex } of [
        { dir: new THREE.Vector3(1, 0, 0), text: '+X', hex: '#FF3838' },
        { dir: new THREE.Vector3(0, 1, 0), text: '+Y', hex: '#3CC98E' },
        { dir: new THREE.Vector3(0, 0, 1), text: '+Z', hex: '#5AA8F0' },
      ]) {
        const s = makeLabel(text, hex)
        s.position.set(dir.x * 1.2, dir.y * 1.2, dir.z * 1.2)
        scene.add(s)
      }

      const bodyGroup = new THREE.Group()
      scene.add(bodyGroup)

      sceneRef.current = scene
      cameraRef.current = camera
      rendererRef.current = renderer
      bodyGroupRef.current = bodyGroup

      const ro = new ResizeObserver(() => {
        const nw = mount.clientWidth
        const nh = mount.clientHeight
        if (nw <= 0 || nh <= 0) return
        renderer.setSize(nw, nh)
        camera.aspect = nw / nh
        camera.updateProjectionMatrix()
        renderer.render(scene, camera)
      })
      ro.observe(mount)
      disposables.push({ dispose: () => ro.disconnect() })

      new STLLoader().load(maveric3DModelUrl, (loaded: THREE.BufferGeometry) => {
        if (cancelled) {
          loaded.dispose()
          return
        }
        loaded.computeBoundingBox()
        const bb = loaded.boundingBox!
        const center = bb.getCenter(new THREE.Vector3())
        loaded.translate(-center.x, -center.y, -center.z)
        const sz = bb.getSize(new THREE.Vector3())
        const scale = 1.25 / Math.max(sz.x, sz.y, sz.z)
        loaded.scale(scale, scale, scale)

        const material = new THREE.MeshPhongMaterial({
          color: 0xBFC6D1,
          specular: 0x222222,
          shininess: 28,
        })
        const edgesGeom = new THREE.EdgesGeometry(loaded, 28)
        const edgesMat = new THREE.LineBasicMaterial({
          color: 0x000000,
          transparent: true,
          opacity: 0.45,
        })
        bodyGroup.add(new THREE.Mesh(loaded, material))
        bodyGroup.add(new THREE.LineSegments(edgesGeom, edgesMat))
        disposables.push(loaded, material, edgesGeom, edgesMat)
        renderer.render(scene, camera)
      })

      renderer.render(scene, camera)
    })()

    return () => {
      cancelled = true
      disposables.forEach(d => d.dispose())
      rendererRef.current?.dispose()
      const el = rendererRef.current?.domElement
      if (el?.parentNode) el.parentNode.removeChild(el)
      rendererRef.current = null
      sceneRef.current = null
      cameraRef.current = null
      bodyGroupRef.current = null
    }
  }, [])

  useEffect(() => {
    const bg = bodyGroupRef.current
    const r = rendererRef.current
    const s = sceneRef.current
    const c = cameraRef.current
    if (!bg || !r || !s || !c) return

    if (q && q.length === 4 && q.every(v => typeof v === 'number')) {
      const [q0, q1, q2, q3] = q
      const norm = Math.hypot(q0, q1, q2, q3)
      if (norm > 1e-6) {
        // TODO(GNC): confirm scalar-first convention; if attitude renders
        // inverted on first real Q, swap to .set(q0/n, q1/n, q2/n, q3/n).
        bg.quaternion.set(q1 / norm, q2 / norm, q3 / norm, q0 / norm)
      } else {
        bg.quaternion.identity()
      }
    } else {
      bg.quaternion.identity()
    }
    r.render(s, c)
  }, [q])

  // Tab keep-alive: when GNC is hidden via display:none and re-shown, the
  // WebGL drawing buffer can be reclaimed (no preserveDrawingBuffer) and
  // ResizeObserver doesn't fire if the box returns to the same W×H. Re-fit
  // and force a render whenever this subtree becomes active again.
  useEffect(() => {
    if (!tabActive) return
    const mount = mountRef.current
    const r = rendererRef.current
    const s = sceneRef.current
    const c = cameraRef.current
    if (!mount || !r || !s || !c) return
    const w = mount.clientWidth
    const h = mount.clientHeight
    if (w <= 0 || h <= 0) return
    r.setSize(w, h)
    c.aspect = w / h
    c.updateProjectionMatrix()
    r.render(s, c)
  }, [tabActive])

  return (
    <div
      ref={mountRef}
      style={{ width: '100%', height: '100%', position: 'relative' }}
    />
  )
}
