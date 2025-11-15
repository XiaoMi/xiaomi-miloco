## v0.1.12
### Changed
- Update camera filter logic
### Fixed
- Fix spec value-range parse error

## v0.1.11
### Fixed
- Fix miot device mcp judgement error
- Update libmiot_camera to 0.1.5, remove useless log
- Perf mcp tool description

## v0.1.10
### Changed
- Use deepcopy for camere instance create
- Add License text to code header
- Perf mcp logic, adjust the MCP description and add type conversion to the input value
### Fixed
- Fix owner_id format parse error

## v0.1.9
### Changed
- Update libmiot_camera to 0.1.4, build with static lib(zlib, v1.3.1)

## v0.1.8
### Changed
- Update libmiot_camera to 0.1.3, fix camera streaming security issue
- Add dirty logic for camera status
### Fixed
- Fix MCP(send_get_rpc_async) result format error

## v0.1.7
### Changed
- Update libmiot_camera to 0.1.2
- - update libopenssl to 3.2.6
- - update libcurl to 8.16.0

## v0.1.6
### Changed
- Update camera callback logic
- Update camera reconnect logic
- Perform MIoT device mcp init logic
- setup.py get version from pyproject.toml
- Perf miot_camera logic
### Fixed
- Fix pylint warning
- Fix ctrl rpc code check
- Get device list is incomplete
- Fix pip package index
- Fix decoder log print error
- Fix camera destroy fault

## v0.1.5
### Changed
- Remove unnecessary code
- Code structure optimization

## v0.1.4
### Added
- Move the decoding task from C to Python
### Changed
- Code structure optimization
- Code formatting optimization
- remove unnecessary code

## v0.1.3
### Added
- Code structure optimization
- Support camera reconnect (reconnect interval 15s)

## v0.1.2
### Added
- Unify HA Automation names

## v0.1.0
### Added
- Support device spec capability analysis
- Integrate the MCP server module

## v0.0.14
### Added
- Support for pulling camera audio data
- Support for Home Assistant OAuth2 login

## v0.0.13
### Added
- Add force_h264 to force the use of the H.264 format

## v0.0.12
### Added
- Add a verification interface to HA

## v0.0.11
### Added
- Add a field for family rooms

## v0.0.10
### Added
- Support getting device icons

## v0.0.9
### Added
- Support ha rest api
### Changed
- Modify the notification logic of the Mi Home app to support porn and illegal content review

## v0.0.8
### Added
- Support multi-channel streaming, decoding, and encoding of cameras

## v0.0.5
### Changed
- Support multi-channel streaming, decoding, and encoding of cameras

## v0.0.2
### Added
- Supports login, pulling Mi Home devices, pulling and executing Mi Home scenarios, and sending Mi Home notifications