import SwiftUI

struct SearchOverlay: View {
    @EnvironmentObject var appState: AppState
    @ObservedObject var viewModel: SearchViewModel
    @FocusState private var isFocused: Bool

    var body: some View {
        ZStack {
            // Backdrop
            Color.black.opacity(0.3)
                .ignoresSafeArea()
                .onTapGesture {
                    dismiss()
                }

            // Search panel
            VStack(spacing: 0) {
                // Search input
                HStack(spacing: 10) {
                    Image(systemName: "magnifyingglass")
                        .font(.title3)
                        .foregroundStyle(.secondary)

                    TextField("Search sessions, projects, agents...", text: $viewModel.query)
                        .textFieldStyle(.plain)
                        .font(.title3)
                        .focused($isFocused)
                        .onSubmit {
                            Task { await viewModel.search() }
                        }

                    if viewModel.isSearching {
                        ProgressView()
                            .scaleEffect(0.7)
                    }

                    Button("Esc") {
                        dismiss()
                    }
                    .buttonStyle(.plain)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(.quaternary)
                    .cornerRadius(4)
                }
                .padding(.horizontal, 20)
                .padding(.vertical, 14)

                Divider()

                // Results
                if !viewModel.results.isEmpty {
                    ScrollView {
                        LazyVStack(alignment: .leading, spacing: 0) {
                            ForEach(viewModel.groupedResults, id: \.0) { type, items in
                                Section {
                                    ForEach(items) { item in
                                        SearchResultRow(item: item)
                                            .onTapGesture {
                                                viewModel.selectResult(item)
                                            }
                                    }
                                } header: {
                                    HStack(spacing: 6) {
                                        Image(systemName: type.systemImage)
                                            .font(.caption)
                                        Text(type.label)
                                            .font(.caption)
                                            .fontWeight(.semibold)
                                    }
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 20)
                                    .padding(.vertical, 6)
                                }
                            }
                        }
                    }
                    .frame(maxHeight: 400)
                } else if !viewModel.query.isEmpty && !viewModel.isSearching {
                    VStack(spacing: 8) {
                        Image(systemName: "magnifyingglass")
                            .font(.title)
                            .foregroundStyle(.tertiary)
                        Text("No results found")
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                    }
                    .frame(height: 120)
                    .frame(maxWidth: .infinity)
                }
            }
            .background(.regularMaterial)
            .cornerRadius(12)
            .shadow(color: .black.opacity(0.2), radius: 20, y: 10)
            .frame(width: 600)
            .frame(maxHeight: 500)
            .padding(.top, 80)
            .frame(maxHeight: .infinity, alignment: .top)
        }
        .onAppear {
            isFocused = true
        }
        .onKeyPress(.escape) {
            dismiss()
            return .handled
        }
    }

    private func dismiss() {
        viewModel.clear()
        appState.showSearchOverlay = false
    }
}

struct SearchResultRow: View {
    let item: SearchResultItem

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: item.type.systemImage)
                .font(.caption)
                .foregroundStyle(.secondary)
                .frame(width: 20)

            VStack(alignment: .leading, spacing: 1) {
                Text(item.title)
                    .font(.subheadline)
                    .lineLimit(1)
                if let subtitle = item.subtitle {
                    Text(subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
            }

            Spacer()

            if let score = item.score {
                Text(String(format: "%.0f%%", score * 100))
                    .font(.caption2)
                    .foregroundStyle(.tertiary)
                    .monospacedDigit()
            }
        }
        .padding(.horizontal, 20)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .background(Color.clear)
    }
}
