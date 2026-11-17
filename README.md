# ElePyRevitExtension (reorganised)

Install: remove the old extension folders from your pyRevit extensions path
(ElectricalAutomation, EleExtension, ElecAutoCircuit, MEPLevelFixer, VisManager,
MEPClashDetector, CircuitBreakerTools, PanelScheduleManager) and copy in
EleTools.extension + RenderManager.extension. Restart Revit (RenderManager
registers a dockable panel, which needs a restart, not just a reload).

Electrical tab
  Circuiting ........ Auto Circuit | Transfer Load Name
  Panel Schedules ... Panel Schedule Manager | Fill Spare/Space | Panel Schedule Sync | Load Breakdown
  Single Line ....... Auto Number | Feed From Panel | Transfer Parameter | Manual Transfer |
                      Load Transfer | Find Related Detail Item | Cross Check MEP
  Fixtures .......... Auto-Place Fixtures | Copy Host Parameters

MEP Tools tab
  Containment ....... Category Route | Connect | Connect Offset Trays | [Split with Gap / Rotate Trays / Tray Tags]
  Coordination ...... Clash Detector | Scope Check | Fix MEP Levels | MEP Search
  Views ............. Visibility Manager | 3D Section Box | Linked Rooms 3D Views | Filled Region from Linked Rooms
  Data & Export ..... Excel to Revit MEP | Export Manager | PDF & DWG Exporter

Render Manager tab (unchanged, separate extension)
