import { Badge } from './Badge'
import { Icon } from './Icon'

/** Marks a row an external AI agent (an MCP token) wrote; renders nothing for the user's own. */
export function AgentBadge({ name }: { name: string | null | undefined }) {
  if (!name) return null
  return (
    <span title={`Added by the agent “${name}” over MCP`}>
      <Badge tone="accent" className="normal-case" mono>
        <Icon name="smart_toy" size={11} />
        via agent · {name}
      </Badge>
    </span>
  )
}
