"use client";

import { useEffect, useRef } from "react";
import gsap from "gsap";
import { useGSAP } from "@gsap/react";

gsap.registerPlugin(useGSAP);

const defaultWords = [
  "branding",
  "design",
  "development",
  "ecommerce",
  "mobile apps",
  "packaging",
];

interface WordLoaderProps {
  words?: readonly string[];
  className?: string;
  paused?: boolean;
}

export default function WordLoader({
  words = defaultWords,
  className = "",
  paused = false,
}: WordLoaderProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const timeline = useRef<gsap.core.Timeline | null>(null);
  const isPaused = useRef(paused);

  useEffect(() => {
    isPaused.current = paused;
    const loop = timeline.current;
    if (!loop) return;
    if (paused) {
      const entries =
        containerRef.current?.querySelectorAll<HTMLElement>(
          ".word-loader-item",
        );
      const active = Array.from(entries ?? []).findIndex(
        (entry) => getComputedStyle(entry).visibility === "visible",
      );
      loop.pause(`read-${Math.max(0, active)}`);
    } else loop.play();
  }, [paused]);

  useGSAP(
    () => {
      if (!containerRef.current) return;
      const entries =
        containerRef.current.querySelectorAll<HTMLElement>(".word-loader-item");
      if (!entries.length) return;
      const media = gsap.matchMedia();
      media.add(
        {
          reduced: "(prefers-reduced-motion: reduce)",
          all: "(min-width: 0px)",
        },
        (context) => {
          const reduced = context.conditions?.reduced;
          const stagger = reduced ? 0 : { amount: 0.15, ease: "power2.out" };
          const loop = gsap.timeline({
            repeat: -1,
            paused: isPaused.current,
          });
          timeline.current = loop;
          gsap.set(entries, { autoAlpha: 0 });
          entries.forEach((entry, index) => {
            const chars = entry.querySelectorAll(".char");
            loop
              .set(entry, { autoAlpha: 1 })
              .fromTo(
                chars,
                { opacity: 0, y: reduced ? 0 : 10 },
                {
                  opacity: 1,
                  y: 0,
                  duration: reduced ? 0.2 : 0.35,
                  stagger,
                  ease: "power2.out",
                },
              )
              .addLabel(`read-${index}`)
              .to(
                chars,
                {
                  opacity: 0,
                  y: reduced ? 0 : -5,
                  duration: reduced ? 0.2 : 0.35,
                  stagger,
                  ease: "power2.out",
                },
                "+=2",
              )
              .set(entry, { autoAlpha: 0 });
          });
          if (isPaused.current) loop.pause("read-0");
          return () => {
            loop.kill();
            timeline.current = null;
          };
        },
      );
      return () => media.revert();
    },
    { scope: containerRef, dependencies: [words], revertOnUpdate: true },
  );

  return (
    <div
      ref={containerRef}
      className={`word-loader ${className}`}
      aria-hidden="true"
    >
      {words.map((word, index) => (
        <span key={index} className="word-loader-item" data-fact={word}>
          {word.split(" ").map((part, partIndex) => (
            <span key={partIndex}>
              {partIndex > 0 && " "}
              <span className="word-loader-word">
                {Array.from(part).map((char, charIndex) => (
                  <span key={charIndex} className="char">
                    {char}
                  </span>
                ))}
              </span>
            </span>
          ))}
        </span>
      ))}
    </div>
  );
}
