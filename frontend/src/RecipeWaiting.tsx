import { useState } from "react";
import WordLoader from "@/components/ui/word-loader";
import { CartoonButton } from "@/components/ui/cartoon-button";
import { PixelArt } from "./KitchenArt";
import { foodFacts } from "./foodFacts";

const facts = foodFacts.map((fact) => fact.text);

export default function RecipeWaiting() {
  const [paused, setPaused] = useState(false);

  return (
    <div className="recipe-waiting" data-paused={paused}>
      <div className="waiting-pot" aria-hidden="true">
        <PixelArt kind="pot" steaming />
      </div>
      <p className="waiting-fact-intro" aria-hidden="true">
        Did you know...
      </p>
      <WordLoader words={facts} paused={paused} />
      <CartoonButton
        className="fact-toggle secondary"
        hasHighlight={false}
        aria-label={paused ? "Resume food facts" : "Pause food facts"}
        onClick={() => setPaused((value) => !value)}
      >
        {paused ? "Resume" : "Pause"}
      </CartoonButton>
    </div>
  );
}
