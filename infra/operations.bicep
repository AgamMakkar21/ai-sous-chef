targetScope = 'resourceGroup'

@allowed(['dev', 'prod'])
param environment string

@minLength(1)
param location string

@minLength(1)
param owner string

@description('Existing workspace and linked Insights in the explicitly selected subscription/group.')
@minLength(4)
param workspaceName string

@minLength(1)
param applicationInsightsName string

@discriminator('mode')
type AlertRouting = {
  mode: 'disabled'
} | {
  mode: 'approved'
  @minLength(1)
  actionGroupResourceIds: string[]
  @minLength(1)
  reviewReference: string
}

@description('Owner-reviewed existing action groups only. No receivers or paging routes are created.')
param routing AlertRouting = { mode: 'disabled' }

@description('Explicitly selected signal IDs; approval alone cannot enable every rule.')
param enabledSignals string[] = []

@description('Owner-reviewed overrides by signal ID; defaults are proposals, not approved SLOs.')
param thresholds object = {}

@allowed(['PT5M', 'PT15M'])
param evaluationFrequency string = 'PT5M'

@allowed(['PT15M', 'PT30M', 'PT1H'])
param windowSize string = 'PT15M'

@minValue(1)
@maxValue(3)
param failingPeriods int = 1

@minValue(0)
@maxValue(4)
param severity int = 2

@description('Optional reviewed USD input/output prices per million tokens, keyed by exact reported model ID. Empty means unknown cost.')
param modelPrices object = {}

resource workspace 'Microsoft.OperationalInsights/workspaces@2023-09-01' existing = {
  name: workspaceName
}
resource insights 'Microsoft.Insights/components@2020-02-02' existing = {
  name: applicationInsightsName
}

var prefix = 'ai-sous-chef-${environment}'
var tags = { environment: environment, owner: owner, managedBy: 'bicep' }
var signals = loadJsonContent('../monitoring/signals.json')
var activeSignals = routing.mode == 'approved' ? intersection(enabledSignals, map(signals, signal => signal.id)) : []
var query = replace(
  '${loadTextContent('../monitoring/sources.kql')}\n${loadTextContent('../monitoring/operations.kql')}',
  '__INSIGHTS_RESOURCE_ID__',
  insights.id
)
var pricedQuery = replace(query, '__MODEL_PRICES__', string(modelPrices))
var runbook = 'https://github.com/AgamMakkar21/ai-sous-chef/blob/dev/docs/operations.md'
var panels = [
  { title: 'Application, agent, tool, database and learning health', query: 'Health' }
  { title: 'Model latency, reported tokens and estimated USD (null = unknown)', query: 'Models' }
  { title: 'Learning commit, dedup, replay, quarantine and batch outcomes', query: 'Learning' }
  { title: 'Observed feedback processing lag (not queue depth)', query: 'Lag' }
  { title: 'Reported evaluation regressions', query: 'Evaluation' }
]
var workbookPanels = [
  for panel in panels: {
    type: 3
    name: panel.title
    content: {
      version: 'KqlItem/1.0'
      title: panel.title
      query: '${pricedQuery}\n${panel.query}'
      queryType: 0
      resourceType: 'microsoft.operationalinsights/workspaces'
      crossComponentResources: [workspace.id]
      timeContext: { durationMs: 86400000 }
      visualization: 'table'
      size: 0
    }
  }
]

resource workbook 'Microsoft.Insights/workbooks@2022-04-01' = {
  name: guid(workspace.id, prefix, 'operations-v1')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: '${prefix} operations'
    category: 'workbook'
    sourceId: workspace.id
    version: 'Notebook/1.0'
    serializedData: string({
      version: 'Notebook/1.0'
      isLocked: true
      items: concat([
        {
          type: 1
          name: 'scope-and-limitations'
          content: {
            json: '# ${prefix} operations\nCounts/latency/tokens reflect observed spans, not billing or queue depth. Missing telemetry is unknown, not healthy. Sampling can undercount calls/tokens. Prices require owner review. [Runbooks](${runbook}).'
          }
        }
      ], workbookPanels)
    })
  }
}

resource alerts 'Microsoft.Insights/scheduledQueryRules@2023-12-01' = [for signal in signals: {
  name: '${prefix}-${signal.id}'
  location: location
  tags: tags
  kind: 'LogAlert'
  properties: {
    displayName: '${prefix}: ${signal.id}'
    description: 'Owner-reviewed routing required. Diagnose/recover: ${runbook}#${signal.runbook}'
    enabled: contains(activeSignals, signal.id)
    severity: severity
    scopes: [workspace.id]
    evaluationFrequency: evaluationFrequency
    windowSize: windowSize
    autoMitigate: true
    skipQueryValidation: false
    criteria: {
      allOf: [{
        query: '${pricedQuery}\n${signal.query}'
        metricMeasureColumn: 'Value'
        operator: 'GreaterThanOrEqual'
        threshold: thresholds[?signal.id] ?? signal.threshold
        timeAggregation: 'Maximum'
        failingPeriods: {
          minFailingPeriodsToAlert: failingPeriods
          numberOfEvaluationPeriods: failingPeriods
        }
      }]
    }
    actions: {
      actionGroups: routing.mode == 'approved' ? routing.actionGroupResourceIds : []
      customProperties: {
        environment: environment
        runbookUrl: '${runbook}#${signal.runbook}'
        reviewReference: routing.mode == 'approved' ? routing.reviewReference : 'pending'
      }
    }
  }
}]

output workbookResourceId string = workbook.id
output enabledAlertIds array = activeSignals
