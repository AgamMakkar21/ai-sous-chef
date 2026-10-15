"""Explicit setup/refresh and a read-only, cited Foundry IQ retrieval check."""

import argparse
import sys
from pathlib import Path

from sous_chef.adapters.credentials import build_credential
from sous_chef.adapters.foundry_iq import FoundryRecipeKnowledge, RecipeKnowledgeConfig
from sous_chef.services.recipe_indexing import RecipeIndexingError, load_corpus
from sous_chef.settings import ConfigurationError, load_settings
from sous_chef.telemetry import configure_safe_telemetry


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    refresh = commands.add_parser("refresh", help="upsert a local ASC-28 corpus; writes to Azure")
    refresh.add_argument("--corpus", type=Path, default=Path("data/corpus/recipes.jsonl"))
    refresh.add_argument(
        "--configure",
        action="store_true",
        help="also create/update the configured index and connection",
    )
    retrieve = commands.add_parser("query", help="read-only knowledge-base retrieval")
    retrieve.add_argument("--query", required=True)
    args = parser.parse_args(argv)
    try:
        configure_safe_telemetry()
        settings = load_settings()
        config = RecipeKnowledgeConfig.from_settings(settings)
        # Validate the whole local slice before making any Azure request.
        documents = load_corpus(args.corpus) if args.command == "refresh" else []
        credential = build_credential(settings)
        client = FoundryRecipeKnowledge(config, credential)
        try:
            if args.command == "refresh":
                if args.configure:
                    client.configure()
                count = client.index(documents)
                print(f"Indexed {count} recipes by stable source ID; no duplicate keys created.")
                print("Indexing is asynchronous; run the read-only query to verify retrieval.")
            else:
                citations = client.retrieve(args.query)
                if not citations:
                    raise RecipeIndexingError(
                        "Query returned no recipes; check corpus relevance and indexing completion."
                    )
                for citation in citations:
                    print(
                        f"reference={citation.reference_id} source_id={citation.recipe.source_id} "
                        f"source={citation.source_url}"
                    )
        finally:
            client.close()
            close = getattr(credential, "close", None)
            if close is not None:
                close()
    except (ConfigurationError, RecipeIndexingError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
