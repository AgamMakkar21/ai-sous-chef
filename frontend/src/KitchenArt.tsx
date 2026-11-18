type Produce = "pot" | "tomato" | "lemon" | "avocado" | "carrot" | "broccoli";

const sprites: Record<Produce, { box: string; paths: [string, string][] }> = {
  pot: {
    box: "0 0 24 24",
    paths: [
      ["M8 1h2v3H8zm6 0h2v2h-2zM9 4h2v2H9zm6-1h2v3h-2z", "orange"],
      ["M8 8h8v2h3v2h3v3h-3v5h-3v2H8v-2H5v-5H2v-3h3v-2h3z", "orange"],
      ["M5 12h14v2H5zm3 6h8v2H8z", "warning"],
      ["M17 15h2v5h-2zm-9 5h8v2H8z", "coral"],
      ["M10 15h2v3h-2z", "produce-highlight"],
    ],
  },
  tomato: {
    box: "0 0 24 24",
    paths: [
      ["M11 1h3v5h4v3h-6V7H8V5h3z", "success"],
      ["M5 8h14v2h2v3h1v7h-3v2H6v-2H3v-8h2z", "coral"],
      ["M19 11h2v9h-3v2H7v-2h10v-3h2z", "tomato-shadow"],
      ["M6 10h4v2H8v3H5v-3h1z", "warning"],
      ["M10 6h7v3h-3v2h-3V9H8V7h2z", "success"],
    ],
  },
  lemon: {
    box: "0 0 20 20",
    paths: [
      ["M11 1h4v3h-4z", "success"],
      ["M6 3h8v2h3v3h2v6h-3v3h-4v2H6v-2H3v-3H1V8h2V5h3z", "warning"],
      ["M14 5h3v3h2v6h-3v3h-4v2H6v-2h6v-3h3z", "orange"],
      ["M5 7h3v2H6v4H4V9h1z", "produce-highlight"],
    ],
  },
  carrot: {
    box: "0 0 22 28",
    paths: [
      ["M9 1h3v7H9zM3 3h3v3h3v3H6V7H3zm12-2h3v6h-3v3h-3V7h3z", "success"],
      ["M5 9h12v3h2v4h-3v3h-3v3h-3v3H7v2H4v-7H3v-7h2z", "orange"],
      ["M5 10h6v3H8v3H5z", "warning"],
      [
        "M16 12h3v4h-3v3h-3v3h-3v3H7v2H4v-3h3v-3h3v-3h3v-3h3z",
        "produce-shadow",
      ],
    ],
  },
  avocado: {
    box: "0 0 22 28",
    paths: [
      ["M9 1h5v3h3v5h2v5h2v9h-3v3H5v-3H2v-9h2V9h2V4h3z", "success"],
      ["M9 5h5v5h2v5h2v7h-3v2H7v-2H5v-7h2v-5h2z", "avocado-flesh"],
      ["M9 14h6v3h2v4h-3v2H9v-2H7v-4h2z", "orange"],
      ["M10 16h3v3h-3z", "warning"],
    ],
  },
  broccoli: {
    box: "0 0 26 28",
    paths: [
      ["M11 15h5v10h3v3H8v-3h3z", "success"],
      ["M12 16h2v10h-2z", "stalk-highlight"],
      ["M8 2h7v2h5v3h4v4h2v5h-4v3h-7v-3h-4v3H4v-3H1v-5h3V6h4z", "success"],
      ["M13 6h3v4h4v3h-4v4h-3v-4H9v-3h4z", "produce-detail"],
      ["M20 7h4v4h2v5h-4v3h-7v-3h6v-5h-1z", "leaf-shadow"],
    ],
  },
};

export function PixelArt({ kind }: { kind: Produce }) {
  const sprite = sprites[kind];
  return (
    <svg
      viewBox={sprite.box}
      shapeRendering="crispEdges"
      aria-hidden="true"
      focusable="false"
    >
      {sprite.paths.map(([path, paint]) => (
        <path key={path} d={path} fill={`var(--cp-${paint})`} />
      ))}
    </svg>
  );
}

const icons = {
  groceries: "M5 8h14l1 12H4L5 8Zm3 0V6a4 4 0 0 1 8 0v2",
  "sous-chef":
    "M7 16a5 5 0 0 1-3-9 4 4 0 0 1 5-3 4 4 0 0 1 7 0 4 4 0 0 1 4 6 5 5 0 0 1-3 6v5H7v-5Zm0 1h10",
  "recipe-history": "M3 11a9 9 0 1 1 2 7M3 5v6h6m3-4v5l3 2",
  profile:
    "M5 21v-3a7 7 0 0 1 14 0v3M15.5 8a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0",
  sun: "M15.5 12a3.5 3.5 0 1 1-7 0 3.5 3.5 0 0 1 7 0M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5",
  moon: "M20 15.5A9 9 0 0 1 8.5 4 9 9 0 1 0 20 15.5Z",
  arrow: "M4 12h15m-6-6 6 6-6 6",
  flame:
    "M12 2c1 6 8 7 8 13a8 8 0 0 1-16 0c0-3 2-6 4-8 0 4 2 5 3 6 2-3 2-7 1-11Z",
  trash: "M4 6h16M9 6V3h6v3M6 6l1 15h10l1-15M10 10v7m4-7v7",
};

export function KitchenIcon({ name }: { name: keyof typeof icons }) {
  return (
    <svg
      className="kitchen-icon"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d={icons[name]} />
    </svg>
  );
}

export function KitchenHero() {
  return (
    <section className="kitchen-hero" aria-labelledby="kitchen-title">
      <div className="hero-copy">
        <p className="eyebrow">
          <KitchenIcon name="flame" /> DINNER STARTS HERE
        </p>
        <h1 id="kitchen-title">YOUR PANTRY</h1>
      </div>
      <div className="produce-scene" aria-hidden="true">
        {(
          ["tomato", "lemon", "avocado", "carrot", "broccoli", "pot"] as const
        ).map((kind) => (
          <span className={`floating ${kind}`} key={kind}>
            <PixelArt kind={kind} />
          </span>
        ))}
        <span className="scene-label">good things cooking.</span>
        <span className="spark one">+</span>
        <span className="spark two">+</span>
      </div>
    </section>
  );
}
