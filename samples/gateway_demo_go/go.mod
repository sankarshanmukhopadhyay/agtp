module agtp.io/samples/gateway-demo-go

go 1.21

require (
	agtp.io/agtp-go v0.0.0
	agtp.io/mod-go v0.0.0
)

replace agtp.io/agtp-go => ../../sdk/agtp-go
replace agtp.io/mod-go => ../../runtimes/mod_go
