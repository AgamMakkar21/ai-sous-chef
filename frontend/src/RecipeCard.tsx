import { useId } from "react";
import type { ReactNode } from "react";
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
  missing_quantity: "quantity unavailable; please specify an amount",
  missing_unit: "unit unavailable; please specify a measurement",
  unsupported_unit: "no supported weight conversion; please specify a weight",
  food_unavailable: "source food unavailable",
  lookup_limit: "source lookup limit reached",
  missing_nutrients: "source nutrients missing",
  service_unavailable: "USDA temporarily unavailable",
};
const calorieSources = {
  usda_mcp: "USDA",
  web_iq: "Web estimate",
  ai_knowledge: "AI estimate",
};
const nutrientNames = Object.keys(
  nutrientLabels,
) as (keyof typeof nutrientLabels)[];
const inventoryLabels = {
  available: "Already have",
  missing: "Need to buy",
  unknown: "Check your pantry",
};

export default function RecipeCard({
  recipe,
  actions,
}: {
  recipe: StructuredRecipe;
  actions?: ReactNode;
}) {
  const id = useId();
  const nutrition = recipe.nutrition;
  const ingredientName = (lineId: string) =>
    recipe.ingredients.find((line) => line.lineId === lineId)!.name;
  const sources = recipe.sources.filter(
    (source) => source.kind === "curated_recipe",
  );
  const nutritionRows = recipe.ingredients.map((ingredient) => {
    const mapping = nutrition.mappings.find(
      (item) => item.ingredientLineId === ingredient.lineId,
    );
    const source = nutrition.sources.find(
      (item) => item.sourceId === mapping?.sourceId,
    );
    const calories = nutrition.ingredientCalories?.find(
      (item) => item.ingredientLineId === ingredient.lineId,
    );
    const estimates = (nutrition.nutrientEstimates ?? []).filter(
      (item) => item.ingredientLineId === ingredient.lineId,
    );
    return {
      ingredient,
      mapping,
      reference:
        source?.reference ??
        calories?.reference ??
        estimates.find((item) => item.reference)?.reference,
      values: nutrientNames.map((name) => {
        if (name === "energy" && calories) return { ...calories, unit: "kcal" };
        const nutrient = mapping?.nutrients?.find((item) => item.name === name);
        if (nutrient)
          return {
            ...nutrient,
            source: "usda_mcp" as const,
            reference: source?.reference,
          };
        const estimate = estimates.find((item) =>
          item.nutrients.some((nutrient) => nutrient.name === name),
        );
        const estimatedNutrient = estimate?.nutrients.find(
          (item) => item.name === name,
        );
        return estimate && estimatedNutrient
          ? {
              ...estimatedNutrient,
              source: estimate.source,
              reference: estimate.reference,
            }
          : null;
      }),
    };
  });

  return (
    <section className="recipe-card" aria-labelledby={`${id}-title`}>
      <header>
        <h3 id={`${id}-title`}>{recipe.title}</h3>
        {recipe.generationSource === "ai_knowledge" && (
          <p className="notice">
            AI-knowledge recipe: recipe sources were unavailable. This recipe is
            not externally verified; nutrition is attributed in Nutrition
            details.
          </p>
        )}
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

      {!!recipe.fitNotes?.length && (
        <section aria-labelledby={`${id}-notes`}>
          <h4 id={`${id}-notes`}>Recipe notes</h4>
          {recipe.fitNotes.map((note, index) => (
            <p className="notice" key={index}>
              {note}
            </p>
          ))}
        </section>
      )}

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
        <ul className="recipe-ingredients">
          {recipe.ingredients.map((line) => {
            const purchase = recipe.shoppingItems.find(
              (item) => item.ingredientLineId === line.lineId,
            );
            return (
              <li key={line.lineId}>
                {quantity(line.quantity, line.unit)} - {line.name} -{" "}
                {inventoryLabels[line.inventoryStatus]}
                {purchase &&
                  (purchase.quantity !== line.quantity ||
                    purchase.unit !== line.unit) && (
                    <span className="recipe-quantity">
                      Buy {quantity(purchase.quantity, purchase.unit)}.
                    </span>
                  )}
              </li>
            );
          })}
        </ul>
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
        <div
          className="recipe-nutrition-tags"
          role="group"
          aria-label="Nutrition status"
        >
          {nutrition.status !== "known" && (
            <span className="recipe-source-tag">
              {nutrition.status === "partial"
                ? "Partial estimate"
                : "Nutrition unavailable"}
            </span>
          )}
          {nutrition.mappings.some((item) => item.quantityEstimate) && (
            <span className="recipe-source-tag">Estimated quantities</span>
          )}
        </div>
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
        <details className="recipe-nutrition-details">
          <summary>Nutrition details</summary>
          <div
            className="recipe-nutrition-scroll"
            role="region"
            aria-label="Ingredient nutrition"
            tabIndex={0}
          >
            <table className="recipe-nutrition-table">
              <caption>Nutrition by ingredient (whole recipe)</caption>
              <thead>
                <tr>
                  <th scope="col">Ingredient</th>
                  {nutrientNames.map((name) => (
                    <th scope="col" key={name}>
                      {nutrientLabels[name]}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {nutritionRows.map(
                  ({ ingredient, mapping, reference, values }) => (
                    <tr key={ingredient.lineId}>
                      <th scope="row">
                        {reference ? (
                          <Reference
                            reference={reference}
                            label={ingredient.name}
                          />
                        ) : (
                          ingredient.name
                        )}
                        {mapping && (
                          <span className="recipe-nutrition-source">
                            USDA
                            {mapping.approximateMatch &&
                              mapping.foodDescription &&
                              ` / Estimated match: ${mapping.foodDescription}`}
                          </span>
                        )}
                        {mapping?.quantityEstimate && (
                          <span className="recipe-nutrition-source">
                            Estimated quantity
                            {mapping.quantityEstimate.sources.map((source) => (
                              <span key={source.reference}>
                                {" / "}
                                <Reference
                                  reference={source.reference}
                                  label="Quantity estimate source"
                                />
                              </span>
                            ))}
                          </span>
                        )}
                      </th>
                      {values.map((value, index) => (
                        <td key={nutrientNames[index]}>
                          {value ? (
                            <>
                              {value.amount.toLocaleString(undefined, {
                                maximumFractionDigits: 2,
                              })}{" "}
                              {value.unit}
                              {value.source !== "usda_mcp" && (
                                <span className="recipe-nutrition-source">
                                  {value.reference ? (
                                    <Reference
                                      reference={value.reference}
                                      label={calorieSources[value.source]}
                                    />
                                  ) : (
                                    calorieSources[value.source]
                                  )}
                                </span>
                              )}
                            </>
                          ) : (
                            "Unavailable"
                          )}
                        </td>
                      ))}
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </div>
          {!!nutrition.coverage?.some(
            (item) => item.unknownIngredientLineIds.length,
          ) && (
            <ul className="recipe-coverage">
              {nutrition.coverage
                .filter((item) => item.unknownIngredientLineIds.length)
                .map((item) => (
                  <li key={item.name}>
                    {nutrientLabels[item.name]} unavailable for:{" "}
                    {item.unknownIngredientLineIds
                      .map(ingredientName)
                      .join(", ")}
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
        </details>
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
        {actions}
      </footer>
    </section>
  );
}
