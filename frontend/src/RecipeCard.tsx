import { useId } from "react";
import {
  nutrientLabels,
  safeRecipeUrl,
  type StructuredRecipe,
} from "./recipes";

function Reference({ reference, label }: { reference: string; label: string }) {
  const href = safeRecipeUrl(reference);
  return href ? (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      referrerPolicy="no-referrer"
    >
      {label} <span className="sr-only">(opens in a new tab)</span>
    </a>
  ) : (
    <span>{label} (link unavailable)</span>
  );
}

function quantity(quantity: number | null, unit: string | null) {
  if (quantity === null)
    return `Quantity unavailable${unit ? ` (${unit})` : ""}`;
  return `${quantity.toLocaleString()} ${unit ?? "(unit unavailable)"}`;
}

const unknownReasons: Record<string, string> = {
  missing_quantity: "quantity unavailable",
  missing_unit: "unit unavailable",
  unsupported_unit: "no supported weight conversion",
  food_unavailable: "source food unavailable",
  lookup_limit: "source lookup limit reached",
  missing_nutrients: "source nutrients missing",
};

export default function RecipeCard({ recipe }: { recipe: StructuredRecipe }) {
  const id = useId();
  const nutrition = recipe.nutrition;
  const groups = [
    { title: "Already have", status: "available" },
    { title: "Need to buy", status: "missing" },
    { title: "Check your pantry", status: "unknown" },
  ];
  const ingredientName = (lineId: string) =>
    recipe.ingredients.find((line) => line.lineId === lineId)!.name;
  const sources = recipe.sources.filter(
    (source) => source.kind === "curated_recipe",
  );

  return (
    <section className="recipe-card" aria-labelledby={`${id}-title`}>
      <header>
        <h3 id={`${id}-title`}>{recipe.title}</h3>
        <dl className="recipe-meta">
          <div>
            <dt>Total time</dt>
            <dd>{recipe.totalMinutes} min</dd>
          </div>
          <div>
            <dt>Skill</dt>
            <dd className="recipe-skill">{recipe.skill}</dd>
          </div>
          <div>
            <dt>Servings</dt>
            <dd>{recipe.servings}</dd>
          </div>
        </dl>
      </header>

      <section aria-labelledby={`${id}-fit`}>
        <h4 id={`${id}-fit`}>Why this fits</h4>
        <ul>
          {recipe.fitReasons.map((reason, index) => (
            <li key={index}>{reason}</li>
          ))}
        </ul>
      </section>

      <section aria-labelledby={`${id}-ingredients`}>
        <h4 id={`${id}-ingredients`}>Ingredients</h4>
        <div className="recipe-ingredients">
          {groups.map((group) => {
            const lines = recipe.ingredients.filter(
              (line) => line.inventoryStatus === group.status,
            );
            if (group.status === "unknown" && !lines.length) return null;
            return (
              <section key={group.status} aria-label={group.title}>
                <h5>{group.title}</h5>
                {group.status === "unknown" && (
                  <p>Availability is unknown; check before shopping.</p>
                )}
                {lines.length ? (
                  <ul>
                    {lines.map((line) => {
                      const purchase =
                        group.status === "missing"
                          ? recipe.shoppingItems.find(
                              (item) => item.ingredientLineId === line.lineId,
                            )!
                          : line;
                      return (
                        <li key={line.lineId}>
                          <span>{line.name}</span>
                          <span className="recipe-quantity">
                            {quantity(purchase.quantity, purchase.unit)}
                          </span>
                          {group.status === "missing" &&
                            purchase.quantity !== line.quantity && (
                              <span className="recipe-quantity">
                                Recipe needs{" "}
                                {quantity(line.quantity, line.unit)} in total
                              </span>
                            )}
                        </li>
                      );
                    })}
                  </ul>
                ) : (
                  <p>
                    {group.status === "available"
                      ? "None confirmed in your groceries."
                      : "Nothing to buy."}
                  </p>
                )}
              </section>
            );
          })}
        </div>
      </section>

      <section aria-labelledby={`${id}-steps`}>
        <h4 id={`${id}-steps`}>Cooking steps</h4>
        <ol className="recipe-steps">
          {recipe.instructions.map((step) => (
            <li key={step.step}>{step.text}</li>
          ))}
        </ol>
      </section>

      <section aria-labelledby={`${id}-nutrition`}>
        <h4 id={`${id}-nutrition`}>Nutrition estimates</h4>
        <p>
          {nutrition.servingBasis === "per_serving"
            ? "Per serving"
            : "Whole recipe"}{" "}
          / {recipe.servings} servings
        </p>
        {nutrition.status === "partial" && (
          <p>
            Incomplete estimate. Partial amounts cover only known ingredients,
            not the full recipe.
          </p>
        )}
        {nutrition.status === "unavailable" && (
          <p>Nutrition is unavailable. Missing values are not zero.</p>
        )}
        <dl className="recipe-nutrients">
          {Object.entries(nutrientLabels).map(([name, label]) => {
            const nutrient = nutrition.nutrients.find(
              (item) => item.name === name,
            );
            const coverage = nutrition.coverage?.find(
              (item) => item.name === name,
            );
            const partial = coverage
              ? coverage.status === "partial"
              : nutrition.status === "partial";
            return (
              <div key={name}>
                <dt>{label}</dt>
                <dd>
                  {nutrient
                    ? `${partial ? "Partial: " : ""}${nutrient.amount.toLocaleString()} ${nutrient.unit}`
                    : "Unavailable"}
                </dd>
              </div>
            );
          })}
        </dl>
        {!!nutrition.coverage?.some(
          (item) => item.unknownIngredientLineIds.length,
        ) && (
          <ul className="recipe-coverage">
            {nutrition.coverage
              .filter((item) => item.unknownIngredientLineIds.length)
              .map((item) => (
                <li key={item.name}>
                  {nutrientLabels[item.name]} unavailable for:{" "}
                  {item.unknownIngredientLineIds.map(ingredientName).join(", ")}
                  .
                </li>
              ))}
          </ul>
        )}
        {!!nutrition.unknownComponents?.length && (
          <ul>
            {nutrition.unknownComponents.map((item, index) => (
              <li key={index}>
                {ingredientName(item.ingredientLineId)}:{" "}
                {unknownReasons[item.reason]} (
                {item.unknownNutrients
                  .map((name) => nutrientLabels[name])
                  .join(", ")}
                ).
              </li>
            ))}
          </ul>
        )}
        <p className="recipe-disclaimer">{nutrition.notice}</p>
        {nutrition.sources.length > 0 && (
          <>
            <h5>Nutrition sources</h5>
            <ul>
              {nutrition.sources.map((source) => (
                <li key={source.sourceId}>
                  <Reference
                    reference={source.reference}
                    label={`USDA / ${source.sourceId}`}
                  />{" "}
                  / Version: {source.version}
                  <ul>
                    {nutrition.mappings
                      .filter((item) => item.sourceId === source.sourceId)
                      .map((item) => (
                        <li key={item.ingredientLineId}>
                          {ingredientName(item.ingredientLineId)} / Food ID:{" "}
                          {item.usdaFoodId}
                        </li>
                      ))}
                  </ul>
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section aria-labelledby={`${id}-sources`}>
        <h4 id={`${id}-sources`}>Grounded recipe sources</h4>
        <p>
          Citations describe the source recipe, not verification of creative
          changes.
        </p>
        <ul>
          {sources.map((source) => (
            <li key={source.sourceId}>
              <Reference reference={source.reference} label={source.sourceId} />{" "}
              / Version: {source.version}
              {recipe.sourceClaims
                .filter((claim) => claim.sourceId === source.sourceId)
                .map((claim, index) => (
                  <blockquote key={index}>{claim.quote}</blockquote>
                ))}
            </li>
          ))}
        </ul>
      </section>
      {(recipe.adaptations.length > 0 || recipe.substitutions.length > 0) && (
        <section aria-labelledby={`${id}-adaptations`}>
          <h4 id={`${id}-adaptations`}>Creative adaptations</h4>
          <p>Generated changes, not claims from the cited recipe.</p>
          <ul>
            {recipe.adaptations.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
            {recipe.substitutions.map((item, index) => (
              <li key={`sub-${index}`}>
                {ingredientName(item.ingredientLineId)} instead of{" "}
                {item.replaces}: {item.reason}
              </li>
            ))}
          </ul>
        </section>
      )}
      {recipe.webGrounding && (
        <section aria-labelledby={`${id}-web`}>
          <h4 id={`${id}-web`}>Web context</h4>
          <p>
            Additional web guidance, separate from curated recipe and USDA
            evidence.
          </p>
          <ul>
            {recipe.webGrounding.claims.map((claim, index) => (
              <li key={index}>
                {claim.text}{" "}
                <Reference
                  reference={claim.sourceUrl}
                  label={
                    recipe.webGrounding!.citations.find(
                      (citation) => citation.url === claim.sourceUrl,
                    )!.title
                  }
                />
              </li>
            ))}
          </ul>
          <p>
            Retrieved:{" "}
            <time dateTime={recipe.webGrounding.retrievedAt}>
              {new Date(recipe.webGrounding.retrievedAt).toLocaleString()}
            </time>
          </p>
        </section>
      )}

      <footer>
        <p className="recipe-disclaimer">
          Check ingredient labels and handling guidance. No recipe can guarantee
          allergy safety.
        </p>
        <div className="recipe-actions">
          <button
            type="button"
            className="button primary"
            disabled
            aria-describedby={`${id}-actions`}
          >
            Cook This
          </button>
          <button
            type="button"
            className="button secondary"
            disabled
            aria-describedby={`${id}-actions`}
          >
            Save to Cook
          </button>
        </div>
        <p id={`${id}-actions`}>
          Cooking and saving actions are not available in this view yet. This
          recipe has not been saved or marked cooked.
        </p>
      </footer>
    </section>
  );
}
