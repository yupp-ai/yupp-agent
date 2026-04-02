import SwiftUI

struct SessionHeaderView: View {
    let session: Session?
    let agent: Agent?

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 10) {
                // Agent icon
                Image(systemName: agent?.executorType == "harnessed" ? "cpu" : "terminal")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)

                // Agent name
                Text(session?.displayAgent ?? "Loading...")
                    .font(.subheadline)
                    .fontWeight(.medium)

                // Model badge
                if let model = agent?.displayModel {
                    ModelBadge(model: model)
                }

                // Executor type
                if let execType = agent?.executorType {
                    Text(execType)
                        .font(.caption2)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(.quaternary)
                        .cornerRadius(4)
                }

                Spacer()

                // Status
                if let status = session?.status {
                    HStack(spacing: 4) {
                        StatusDot(status: status, size: 6)
                        Text(status.label)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 4)

            // Second row: full session ID + links
            if let sid = session?.sessionId {
                HStack(spacing: 10) {
                    // Full session ID (clickable to copy)
                    Button {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(sid, forType: .string)
                    } label: {
                        HStack(spacing: 4) {
                            Text(sid)
                                .font(.system(size: 10, design: .monospaced))
                                .foregroundStyle(.tertiary)
                            Image(systemName: "doc.on.doc")
                                .font(.system(size: 9))
                                .foregroundStyle(.tertiary)
                        }
                    }
                    .buttonStyle(.plain)
                    .help("Copy session ID")

                    Spacer()

                    // Lit console link
                    Link(destination: URL(string: "http://lit.yupp.ai/agent_harness_console?session_id=\(sid)")!) {
                        HStack(spacing: 3) {
                            Image(systemName: "flame")
                                .font(.system(size: 9))
                            Text("Lit")
                                .font(.caption2)
                        }
                        .foregroundStyle(.secondary)
                    }
                    .help("Open in Lit console")
                }
                .padding(.horizontal, 16)
                .padding(.bottom, 6)
            }

            Divider()
        }
        .background(Color(nsColor: .textBackgroundColor))
    }
}
