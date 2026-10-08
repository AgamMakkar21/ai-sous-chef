# ai-sous-chef

AI Sous Chef combines current groceries, grounded recipe generation, and
feedback-based personalization.

## MVP contracts

- [Architecture and implementation decisions](docs/adr/0001-mvp-contracts.md)
- [API, schemas, limits, and lifecycle rules](docs/contracts/api-and-schemas.md)
- [Machine-readable contract fixtures](docs/contracts/api-and-schemas.md#9-machine-readable-contract-fixtures)
- [Detailed technical overview](docs/references/technical-overview.md)
- [Original architecture diagram (PDF)](docs/references/architecture-diagram.pdf)

[![AI Sous Chef architecture: Entra authentication, App Service, Foundry agents and tools, Cosmos, asynchronous learning, and monitoring](docs/references/architecture-diagram.png)](docs/references/architecture-diagram.pdf)

These are implementation contracts, not a deployed application. Preference
learning and conditional Web IQ are both MVP requirements. Work starts from
`dev` on a feature/fix branch, enters `dev` through a peer-reviewed PR, and is
promoted to `main` through a separately reviewed PR. Integration into `dev`
does not by itself complete or close the delivery issue.