import { useEffect, useRef } from "react"

// The reasoning thread of the design system: three harmonics of a sine wave, tapered to rest at both ends, drawn on a canvas.
type Props = { width?: number; height?: number; amp?: number; freq?: number; speed?: number; tone?: "spectrum" | "cyan" | "emerald" | "violet" | "ink"; label?: string }

const TONES: Record<NonNullable<Props["tone"]>, string[]> = {
  spectrum: ["#38bdf8", "#a855f7", "#f59e0b"],
  cyan: ["#38bdf8"],
  emerald: ["#10b981"],
  violet: ["#a855f7"],
  ink: ["#e4e4e7"],
}

export function NeuralThread({ width = 64, height = 18, amp = 5, freq = 0.07, speed = 1, tone = "spectrum", label = "Working" }: Props) {
  const ref = useRef<HTMLCanvasElement>(null)

  useEffect(() => {
    const canvas = ref.current
    const ctx = canvas?.getContext("2d")
    if (!canvas || !ctx) return
    const dpr = window.devicePixelRatio || 1
    canvas.width = width * dpr
    canvas.height = height * dpr
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches
    const colors = TONES[tone]
    let raf = 0
    let t = 0

    const draw = () => {
      ctx.clearRect(0, 0, width, height)
      ctx.beginPath()
      ctx.lineWidth = 1.3
      ctx.lineCap = "round"
      ctx.lineJoin = "round"
      const points = 48
      for (let i = 0; i <= points; i++) {
        const x = (i / points) * width
        const envelope = Math.sin((i / points) * Math.PI)
        const y1 = Math.sin(x * freq + t) * amp
        const y2 = Math.sin(x * freq * 2.3 - t * 1.4) * amp * 0.35
        const y3 = Math.cos(x * freq * 0.7 + t * 0.8) * amp * 0.2
        const y = height / 2 + (y1 + y2 + y3) * envelope
        if (i === 0) ctx.moveTo(x, y)
        else ctx.lineTo(x, y)
      }
      const stroke = ctx.createLinearGradient(0, 0, width, 0)
      colors.forEach((c, i) => stroke.addColorStop(colors.length === 1 ? 0 : i / (colors.length - 1), c))
      ctx.strokeStyle = colors.length === 1 ? colors[0] : stroke
      ctx.stroke()
      t += 0.06 * speed
      if (!still) raf = requestAnimationFrame(draw)
    }
    draw()
    return () => cancelAnimationFrame(raf)
  }, [width, height, amp, freq, speed, tone])

  return <canvas ref={ref} className="neural-thread" style={{ width, height }} role="img" aria-label={label} />
}
