import { Fragment, type MutableRefObject, type RefObject } from "react"
import gsap from "gsap"
import { ScrollTrigger } from "gsap/ScrollTrigger"
import { useGSAP } from "@gsap/react"
import Lenis from "lenis"

gsap.registerPlugin(ScrollTrigger, useGSAP)

// Headline split into masked words. The full sentence is the accessible name; the split copy is hidden from readers.
// The space sits outside each clipping word, so the text still reads as a sentence with scripts off.
// "to a *live site.*" sets the starred run in the accent face; the asterisks never reach the page.
function words(line: string) {
  let accent = false
  return line.split(" ").map((raw) => {
    if (raw.startsWith("*")) accent = true
    const word = { text: raw.replace(/\*/g, ""), accent }
    if (raw.endsWith("*")) accent = false
    return word
  })
}

export function Split({ lines }: { lines: string[] }) {
  return (
    <>
      <span className="sr-only">{lines.join(" ").replace(/\*/g, "")}</span>
      <span aria-hidden="true">
        {lines.map((line, i) => (
          <span className="lp-line" key={i}>
            {words(line).map(({ text, accent }, j, all) => (
              <Fragment key={j}>
                <span className="lp-word"><span className={accent ? "is-accent" : undefined}>{text}</span></span>
                {j < all.length - 1 ? " " : ""}
              </Fragment>
            ))}
          </span>
        ))}
      </span>
    </>
  )
}

const clamp01 = (n: number) => Math.min(1, Math.max(0, n))

// One scroll engine (Lenis) on the landing's own scroller, driving GSAP ScrollTrigger. Everything is skipped under
// reduced motion, where the page is plain native scrolling with every final state already showing.
export function useLandingMotion(root: RefObject<HTMLDivElement | null>, lenisRef: MutableRefObject<Lenis | null>, on: boolean) {
  useGSAP(() => {
    const el = root.current
    if (!el || !on) return
    const q = <T extends Element>(sel: string) => gsap.utils.toArray<T>(sel, el)
    const base = { scroller: el }

    // The video stays bright through the hero, goes black while reading, and comes back for the closing call to action.
    const paintFade = () => {
      const hero = el.querySelector<HTMLElement>(".lp-hero")
      const final = el.querySelector<HTMLElement>(".lp-final")
      const span = hero?.offsetHeight ?? el.clientHeight
      let fade = clamp01((el.scrollTop - span * 0.12) / (span * 0.5))
      if (final) {
        const top = final.getBoundingClientRect().top - el.getBoundingClientRect().top
        fade = Math.min(fade, 1 - clamp01(1 - (top - 32) / el.clientHeight))
      }
      el.style.setProperty("--fade", String(fade))
    }
    paintFade()
    el.addEventListener("scroll", paintFade, { passive: true })
    window.history.scrollRestoration = "manual"
    el.scrollTo(0, 0)

    const mm = gsap.matchMedia()

    mm.add("(prefers-reduced-motion: no-preference)", () => {
      const content = el.querySelector<HTMLElement>(".lp-content")
      const lenis = new Lenis({ wrapper: el, content: content ?? undefined, lerp: 0.16, wheelMultiplier: 1.35, smoothWheel: true })
      lenisRef.current = lenis
      lenis.on("scroll", ScrollTrigger.update)
      const tick = (time: number) => lenis.raf(time * 1000)
      gsap.ticker.add(tick)
      gsap.ticker.lagSmoothing(0)

      // Hero: a composed intro, then the block drifts up and out as you scroll away.
      gsap.timeline({ defaults: { ease: "power3.out" } })
        .from(".lp-nav > *", { autoAlpha: 0, y: -14, duration: 0.7, stagger: 0.08 })
        .from(".lp-hero h1 .lp-word > span", { yPercent: 115, duration: 1.1, stagger: 0.07, ease: "power4.out" }, "-=0.4")
        .from(".lp-hero .lp-sub > *", { autoAlpha: 0, y: 18, duration: 0.7, stagger: 0.1 }, "-=0.6")
        .from(".lp-cue", { autoAlpha: 0, duration: 0.8 }, "-=0.3")
      gsap.to(".lp-hero-inner", {
        yPercent: -14, autoAlpha: 0, ease: "none",
        scrollTrigger: { ...base, trigger: ".lp-hero", start: "top top", end: "bottom 15%", scrub: true },
      })

      // Section headings: masked words rise once.
      q<HTMLElement>("[data-split]").forEach((h) => {
        gsap.from(h.querySelectorAll(".lp-word > span"), {
          yPercent: 115, duration: 1, stagger: 0.05, ease: "power4.out",
          scrollTrigger: { ...base, trigger: h, start: "top 88%", once: true },
        })
      })
      q<HTMLElement>("[data-rise]").forEach((node) => {
        gsap.from(node, { autoAlpha: 0, y: 28, duration: 0.9, ease: "power2.out", scrollTrigger: { ...base, trigger: node, start: "top 90%", once: true } })
      })

      // Marquee rows slide against each other, tied to scroll.
      q<HTMLElement>(".lp-marquee-row").forEach((row) => {
        const dir = row.dataset.dir === "1" ? 1 : -1
        gsap.fromTo(row, { xPercent: dir > 0 ? -28 : 0 }, {
          xPercent: dir > 0 ? 0 : -28, ease: "none",
          scrollTrigger: { ...base, trigger: ".lp-marquee", start: "top bottom", end: "bottom top", scrub: true },
        })
      })

      // Cards and index rows stagger in as each batch arrives.
      for (const sel of [".lp-plan"]) {
        gsap.set(sel, { autoAlpha: 0, y: 40 })
        ScrollTrigger.batch(sel, {
          scroller: el, start: "top 92%", once: true,
          onEnter: (batch) => gsap.to(batch, { autoAlpha: 1, y: 0, duration: 0.8, stagger: 0.09, ease: "power3.out" }),
        })
      }

      // Closing call to action: oversized line scales in.
      gsap.from(".lp-final .lp-cta-big", { scale: 0.8, autoAlpha: 0, duration: 1, ease: "back.out(1.6)", scrollTrigger: { ...base, trigger: ".lp-final", start: "top 60%", once: true } })

      const refresh = () => ScrollTrigger.refresh()
      document.fonts?.ready.then(refresh)
      window.addEventListener("load", refresh)

      return () => {
        window.removeEventListener("load", refresh)
        gsap.ticker.remove(tick)
        lenis.destroy()
        lenisRef.current = null
      }
    })

    // The chapters pin, and scrolling steps through them. The section only tells the page which one is current.
    mm.add("(min-width: 769px) and (prefers-reduced-motion: no-preference)", () => {
      const undo = gsap.utils.toArray<HTMLElement>(".lp-chapters", el).map((sec) => {
      const n = sec.querySelectorAll(".lp-chap").length
      sec.classList.add("is-pinned")
      sec.dispatchEvent(new CustomEvent("lp-pinned", { detail: true }))
      let current = -1
      // The new chapter's words arrive: number slides in, the title rises word by word, then the text fades up.
      const arrive = (i: number) => {
        const copy = sec.querySelectorAll<HTMLElement>(".lp-chap-copies .lp-chap-copy")[i]
        if (!copy) return
        gsap.timeline({ defaults: { ease: "power3.out" } })
          .fromTo(copy.querySelector(".lp-chap-num"), { autoAlpha: 0, x: -14 }, { autoAlpha: 1, x: 0, duration: 0.5 })
          .from(copy.querySelectorAll("h3 .lp-word > span"), { yPercent: 115, duration: 0.8, stagger: 0.06, ease: "power4.out" }, "<0.05")
          .from(copy.querySelector("p"), { autoAlpha: 0, y: 14, duration: 0.6 }, "<0.25")
      }
      const trigger = ScrollTrigger.create({
        scroller: el, trigger: sec, pin: true, refreshPriority: Number(sec.dataset.priority ?? 1), start: "top top", end: () => `+=${el.clientHeight * (n > 1 ? (n - 1) * 0.22 : 0.7)}`, anticipatePin: 1, invalidateOnRefresh: true,
        onToggle: (self) => sec.dispatchEvent(new CustomEvent("lp-open", { detail: self.isActive })),
        onUpdate: (self) => {
          const i = n > 1 ? Math.min(n - 1, Math.round(self.progress * (n - 1))) : 0
          if (i !== current) { current = i; arrive(i); sec.dispatchEvent(new CustomEvent("lp-chapter", { detail: i })) }
        },
      })
      const goto = (e: Event) => {
        const i = (e as CustomEvent<number>).detail
        const top = trigger.start + (trigger.end - trigger.start) * (n > 1 ? i / (n - 1) : 0)
        if (lenisRef.current) lenisRef.current.scrollTo(top, { duration: 1.2 }); else el.scrollTo({ top })
      }
      sec.addEventListener("lp-goto", goto)
      const settle = window.setTimeout(() => ScrollTrigger.refresh(), 150)  // the pinned layout changes the section's height
      return () => {
        window.clearTimeout(settle)
        sec.removeEventListener("lp-goto", goto)
        sec.classList.remove("is-pinned")
        sec.dispatchEvent(new CustomEvent("lp-pinned", { detail: false }))
      }
      })
      return () => undo.forEach((fn) => fn())
    })

    // Narrow screens: the moments stay an inline list, each arriving as it is reached.
    mm.add("(max-width: 860px) and (prefers-reduced-motion: no-preference)", () => {
      q<HTMLElement>(".lp-moment").forEach((row) => {
        gsap.set(row.querySelectorAll("p, .lp-moment-num"), { autoAlpha: 0, y: 18 })
        gsap.timeline({ scrollTrigger: { ...base, trigger: row, start: "top 80%", once: true } })
          .from(row.querySelectorAll("h3 .lp-word > span"), { yPercent: 115, duration: 1, stagger: 0.07, ease: "power4.out" })
          .to(row.querySelectorAll("p, .lp-moment-num"), { autoAlpha: 1, y: 0, duration: 0.8, stagger: 0.12, ease: "power3.out" }, "<0.15")
      })
    })

    // Wide screens: the section pins like the chapters, and scrolling brings one moment in at a time.
    mm.add("(min-width: 861px) and (prefers-reduced-motion: no-preference)", () => {
      const sec = el.querySelector<HTMLElement>(".lp-flow")
      if (!sec) return
      const rows = gsap.utils.toArray<HTMLElement>(".lp-moment", sec)
      const screens = gsap.utils.toArray<HTMLElement>(".lp-screen", sec)
      const buttons = gsap.utils.toArray<HTMLElement>(".lp-flow-index button", sec)
      const count = sec.querySelector<HTMLElement>(".lp-stage-count b")
      const ghost = sec.querySelector<HTMLElement>(".lp-flow-ghost")
      const n = rows.length
      sec.classList.add("lp-pin")
      let current = 0
      const mark = (i: number) => {
        rows.forEach((row, j) => row.classList.toggle("is-active", j === i))
        buttons.forEach((button, j) => button.classList.toggle("is-on", j === i))
        if (count) count.textContent = String(i + 1).padStart(2, "0")
      }
      // Everything moves along one horizontal axis: forward, the old moment slides out left and the new one arrives from the right; scrolling back reverses it.
      const go = (to: number, from: number) => {
        const dir = to >= from ? 1 : -1
        const row = rows[to]
        const title = row.querySelector("h3")
        const rest = row.querySelectorAll(".lp-moment-num, p")
        gsap.killTweensOf([...rows, ...screens, title, ...rest, ghost])
        rows.forEach((r, j) => { if (j === from) gsap.to(r, { x: -70 * dir, autoAlpha: 0, duration: 0.5, ease: "power3.in" }); else if (j !== to) gsap.set(r, { autoAlpha: 0 }) })
        screens.forEach((s, j) => { if (j === from) gsap.to(s, { x: -110 * dir, autoAlpha: 0, duration: 0.55, ease: "power3.in" }); else if (j !== to) gsap.set(s, { autoAlpha: 0 }) })
        gsap.fromTo(row, { x: 50 * dir, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.9, ease: "power4.out", delay: 0.2 })
        // the statement moves as one whole line, not word by word
        gsap.fromTo(title, { x: 140 * dir, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 1, ease: "power4.out", delay: 0.22 })
        gsap.fromTo(rest, { x: 36 * dir, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 0.8, stagger: 0.08, ease: "power3.out", delay: 0.4 })
        if (ghost) { ghost.textContent = String(to + 1).padStart(2, "0"); gsap.fromTo(ghost, { x: 240 * dir, autoAlpha: 0 }, { x: 0, autoAlpha: 1, duration: 1.2, ease: "power4.out" }) }
        gsap.fromTo(screens[to], { x: 160 * dir, autoAlpha: 0, scale: 0.97 }, { x: 0, autoAlpha: 1, scale: 1, duration: 1.05, ease: "power4.out", delay: 0.2 })
      }
      gsap.set(rows, { autoAlpha: 0 })
      gsap.set(screens, { autoAlpha: 0 })
      gsap.set([rows[0], screens[0]], { autoAlpha: 1 })
      mark(0)
      const trigger = ScrollTrigger.create({
        scroller: el, trigger: sec, pin: true, refreshPriority: 1, start: "top top", end: () => `+=${el.clientHeight * (n - 1) * 0.3}`, anticipatePin: 1, invalidateOnRefresh: true,
        onToggle: (self) => { if (self.isActive) go(current, -1) },
        onUpdate: (self) => {
          const i = Math.min(n - 1, Math.round(self.progress * (n - 1)))
          if (i !== current) { const from = current; current = i; mark(i); go(i, from) }
        },
      })
      const goto = (e: Event) => {
        const i = (e as CustomEvent<number>).detail
        const top = trigger.start + (trigger.end - trigger.start) * (i / (n - 1))
        if (lenisRef.current) lenisRef.current.scrollTo(top, { duration: 1.2 }); else el.scrollTo({ top })
      }
      sec.addEventListener("lp-flow-goto", goto)
      const settle = window.setTimeout(() => ScrollTrigger.refresh(), 150)  // the pinned layout changes the section's height
      return () => {
        window.clearTimeout(settle)
        sec.removeEventListener("lp-flow-goto", goto)
        sec.classList.remove("lp-pin")
        gsap.set([...rows, ...screens, ghost, ...sec.querySelectorAll(".lp-moment, .lp-moment *")], { clearProps: "opacity,visibility,transform" })
      }
    })

    // Pointer: a glow follows the cursor and the headline lines drift at different depths. Fine pointers only.
    mm.add("(prefers-reduced-motion: no-preference) and (pointer: fine)", () => {
      const hero = el.querySelector<HTMLElement>(".lp-hero")
      const glow = el.querySelector<HTMLElement>(".lp-glow")
      const cta = el.querySelector<HTMLElement>(".lp-hero .landing-cta")
      if (!hero || !glow) return
      const gx = gsap.quickTo(glow, "x", { duration: 0.7, ease: "power3.out" })
      const gy = gsap.quickTo(glow, "y", { duration: 0.7, ease: "power3.out" })
      const lines = q<HTMLElement>(".lp-hero h1 .lp-line")
      const drift = lines.map((line) => gsap.quickTo(line, "x", { duration: 0.9, ease: "power3.out" })).map((fn, i) => (n: number) => fn(n * (i + 1) * 14))
      const mx = cta && gsap.quickTo(cta, "x", { duration: 0.4, ease: "power3.out" })
      const my = cta && gsap.quickTo(cta, "y", { duration: 0.4, ease: "power3.out" })
      const move = (e: PointerEvent) => {
        const r = hero.getBoundingClientRect()
        gx(e.clientX - r.left); gy(e.clientY - r.top)
        const n = (e.clientX - r.left) / r.width - 0.5
        drift.forEach((fn) => fn(n))
      }
      const reset = () => { drift.forEach((fn) => fn(0)); mx?.(0); my?.(0) }
      const hide = () => { if (document.hidden) reset() }
      const onCta = (e: PointerEvent) => {
        const r = cta!.getBoundingClientRect()
        mx?.((e.clientX - r.left - r.width / 2) * 0.25)
        my?.((e.clientY - r.top - r.height / 2) * 0.25)
      }
      hero.addEventListener("pointermove", move)
      hero.addEventListener("pointerleave", reset)
      cta?.addEventListener("pointermove", onCta)
      cta?.addEventListener("pointerleave", reset)
      window.addEventListener("blur", reset)
      document.addEventListener("visibilitychange", hide)
      return () => {
        hero.removeEventListener("pointermove", move)
        hero.removeEventListener("pointerleave", reset)
        cta?.removeEventListener("pointermove", onCta)
        cta?.removeEventListener("pointerleave", reset)
        window.removeEventListener("blur", reset)
        document.removeEventListener("visibilitychange", hide)
      }
    })

    return () => {
      el.removeEventListener("scroll", paintFade)
      mm.revert()
    }
  }, { scope: root, dependencies: [on] })
}
