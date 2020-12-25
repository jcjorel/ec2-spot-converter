# ec2-spot-converter

This tool converts existing EC2 instances back and forth between On-Demand and 'persistent' Spot billing models while preserving
instance attributes (Launch configuration, Tags..), network attributes (existing Private IP addresses, Elastic IP), storage (Volumes),
Elastic Inference accelerators and Elastic GPUs.

It also allows replacement of existing Spot instances with new "identical" ones to update the instance type and cpu options. 

It can also help to fix some Spot instance conditions (Ex: *'IncorrectSpotRequestState Exception'*).

Conversion time ranges from 2 to 5 minutes depending on the instance type.


# Getting started

**Prerequisistes:**
* Install the tool on an EC2 Linux instance located **in the same account and region than the instance to convert**,

```shell
TOOL_VERSION=`curl https://raw.githubusercontent.com/jcjorel/ec2-spot-converter/master/VERSION.txt`
curl https://raw.githubusercontent.com/jcjorel/ec2-spot-converter/master/releases/ec2-spot-converter-${TOOL_VERSION} -o ec2-spot-converter
chmod u+x ec2-spot-converter
```

* Configure an IAM role giving full access to *ec2:*, *dynamodb:* to the EC2 tool instance,
	* *TODO: Refine the required IAM permissions needed*
* Install Python3 and **boto3** package.

## Convert an On-Demand instance to Spot model

```shell
# Initialize the DynamoDB table that will hold conversion states. **DO IT ONLY ONCE PER REGION AND ACCOUNT**.
$ ./ec2-spot-converter --generate-dynamodb-table
Creating DynamoDB table 'ec2-spot-converter-state-table'...

# Convert running On-Demand instance i-0dadf8589b7ec16f6 to Spot model
#   This instance has 3 attached volumes (w/ one multi-attached 'io1' type), 2 ENIs and 1 EIP.
$ ./ec2-spot-converter --stop-instance --review-conversion-result --instance-id i-0dadf8589b7ec16f6
[STEP 1/21] Read DynamoDB state table...
  => SUCCESS. Record 'i-0dadf8589b7ec16f6' doesn't exist yet.
[STEP 2/21] Discover instance state...
  => SUCCESS. Stopping i-0dadf8589b7ec16f6...
[STEP 3/21] Wait for 'stopped' instance state...
Waiting for instance to stop... (current state=stopping)
Waiting for instance to stop... (current state=stopping)
Waiting for instance to stop... (current state=stopping)
  => SUCCESS. Instance in 'stopped' state.
[STEP 4/21] Tag all resources (Instance, ENI(s), Volumes) with ec2-spot-converter job Id...
  => SUCCESS. Successfully tagged ['i-0dadf8589b7ec16f6', 'eni-092172a81811424d5', 'eni-0f33bc507e00eff48', 'vol-05e7ca553ae156cc3', 'vol-081fc97c000b836b2', 'vol-08792eacd354100cb'].
[STEP 5/21] Get storage volume details...
  => SUCCESS. Successfully retrieved volume details for ['vol-05e7ca553ae156cc3', 'vol-081fc97c000b836b2', 'vol-08792eacd354100cb'].
[STEP 6/21] Detach instance volumes with DeleteOnTermination=False...
Detaching volume vol-081fc97c000b836b2...
Detaching volume vol-08792eacd354100cb...
  => SUCCESS. Detached volumes ['vol-081fc97c000b836b2', 'vol-08792eacd354100cb'].
[STEP 7/21] Wait for volume detach status...
Detected multi-attached volume 'vol-08792eacd354100cb'. Taking care of this special case...
Detected multi-attached volume 'vol-08792eacd354100cb'. Taking care of this special case...
  => SUCCESS. All detached volumes are 'available' : ['vol-081fc97c000b836b2', 'vol-08792eacd354100cb'].
[STEP 8/21] Start AMI creation...
AMI Block device mapping: [{'DeviceName': '/dev/xvda', 'Ebs': {'DeleteOnTermination': True, 'VolumeSize': 8, 'VolumeType': 'gp2'}}]
  => SUCCESS. AMI image ec2-spot-converter-i-0dadf8589b7ec16f6/ami-0f1908293d6fba760 started.
[STEP 9/21] Prepare network interfaces for instance disconnection...
  => SUCCESS. Successfully prepared network interfaces ['eni-092172a81811424d5', 'eni-0f33bc507e00eff48'].
[STEP 10/21] Wait for AMI to be ready...
Waiting for image ami-0f1908293d6fba760 to be available...
Waiting for image ami-0f1908293d6fba760 to be available...
  => SUCCESS. AMI ami-0f1908293d6fba760 is ready..
[STEP 11/21] Checkpoint the current exact state of the instance...
  => SUCCESS. Checkpointed instance state...
[STEP 12/21] Terminate instance...
  => SUCCESS. Successfully terminated instance i-0dadf8589b7ec16f6.
[STEP 13/21] Wait resource release...
  => SUCCESS. All resources released : ['eni-092172a81811424d5', 'eni-0f33bc507e00eff48'].
[STEP 14/21] Create new instance...
  => SUCCESS. Created new instance 'i-06236de813ed5bacd'.
[STEP 15/21] Wait new instance to come up...
  => SUCCESS. Instance i-06236de813ed5bacd is in 'running' state.
[STEP 16/21] Reattach volumes...
Attaching volume vol-081fc97c000b836b2 to i-06236de813ed5bacd with device name /dev/sdb...
Attaching volume vol-08792eacd354100cb to i-06236de813ed5bacd with device name /dev/sdf...
  /!\ WARNING /!\ Volumes attached after boot. YOUR NEW INSTANCE MAY NEED A REBOOT!
  => SUCCESS. Successfully reattached volumes ['vol-081fc97c000b836b2', 'vol-08792eacd354100cb']...
[STEP 17/21] Configure network interfaces...
Setting 'DeleteOnTermination=True' for interface eni-092172a81811424d5...
Setting 'DeleteOnTermination=True' for interface eni-0f33bc507e00eff48...
  => SUCCESS. Successfully configured network interfaces ['eni-092172a81811424d5', 'eni-0f33bc507e00eff48'].
[STEP 18/21] Manage Elastic IP...
  => SUCCESS. Reassociated EIPs '['34.247.111.29']'...
[STEP 19/21] Reboot new instance (if needed and requested)...
  => SUCCESS. It is recommend to reboot 'i-06236de813ed5bacd'... Please specify --reboot-if-needed option next time.
[STEP 20/21] Untag resources...
  => SUCCESS. Successfully untagged ['i-06236de813ed5bacd', 'eni-092172a81811424d5', 'eni-0f33bc507e00eff48', 'vol-081fc97c000b836b2', 'vol-08792eacd354100cb'].
[STEP 21/21] Deregister image... => SKIPPED! Need '--delete-ami' argument.
Conversion successful! New instance id: i-06236de813ed5bacd, ElapsedTime: 112 seconds

/!\ WARNING /!\ Volumes attached after boot. YOUR NEW INSTANCE MAY NEED A REBOOT!
```
If the option `--review-conversion-result` is specified, at conversion end, a **VIm Diff** window pops-up and allow interactive review of the differences between
the original now terminated instance and the newly created one: You should see differences only related to dates, AssociationIds, AMI (and storage(s) with 'DeleteOnTermination=True').

![Result review window](review-result.png)

### Convert a Spot instance to On-Demand model

The operation is similar to the Spot model conversion with the difference that the option `--target-billing-model` must be set to `on-demand` explicitly.

> Note: `ec2-spot-converter` can also convert back an instance currently in **Spot interrupted** state to On-Demand model.

### Convert a Spot instance to Spot model to change the instance type and/or CPU Options

`ec2-spot-converter`tool can be used to replace a Spot instance by another one just changing the instance type. This operation is not 
yey possible "in-place" through an AWS EC2 API so the tool will terminate and replace the Spot instance preserving all attributes but
updating the instance type (or CPU options) during the process.

Specify options `--instance-type` and/or `--cpu-options`on an existing Spot instance to start conversion. 

### Fix Spot instance with 'IncorrectSpotRequestState Exception'

The tool is able to fix '*IncorrectSpotRequestState Exceptions*' due to the Spot request been cancelled by the user but the Spot instance is
left running. This kind of instance may suffer some unexpected behaviors like no possibility to stop them anymore.

The tool can be used to recreate a new healthy Spot instance from the problematic Spot instance.

Simply call `ec2-spot-converter` with the problematic Instance Id and specify options --target-instance-type 'spot'
**and --do-not-require-stopped-instance** (as the instance can not be stopped).

# Command line usage

```shell
usage: ec2-spot-converter [-h] -i INSTANCE_ID [-m {spot,on-demand}]
                          [-t TARGET_INSTANCE_TYPE]
                          [--cpu-options CPU_OPTIONS]
                          [--max-spot-price MAX_SPOT_PRICE]
                          [--dynamodb-tablename DYNAMODB_TABLENAME]
                          [--generate-dynamodb-table] [-s]
                          [--reboot-if-needed] [--delete-ami] [-d] [-v]
                          [-r]

EC2 Spot converter v0.1.0 (Tue Dec 22 21:42:24 UTC 2020)

optional arguments:
  -h, --help            show this help message and exit
  -i INSTANCE_ID, --instance-id INSTANCE_ID
                        The id of the EC2 instance to convert.
  -m {spot,on-demand}, --target-billing-model {spot,on-demand}
                        The expected billing model after conversion. Default:
                        'spot'
  -t TARGET_INSTANCE_TYPE, --target-instance-type TARGET_INSTANCE_TYPE
                        The expected instance type (ex: m5.large...) after
                        conversion. This flag is only useful when applied to a
                        Spot instance as EC2.modify_instance_attribute() can't
                        be used to change Instance type. Default:
                        <original_instance_type>
  --cpu-options CPU_OPTIONS
                        Instance CPU Options JSON structure. Format:
                        {"CoreCount":123,"ThreadsPerCore":123}.
  --max-spot-price MAX_SPOT_PRICE
                        Maximum hourly price for Spot instance target.
  --dynamodb-tablename DYNAMODB_TABLENAME
                        A DynamoDB table name to hold conversion states.
                        Default: 'ec2-spot-converter-state-table'
  --generate-dynamodb-table
                        Generate a DynamoDB table name to hold conversion
                        states.
  -s, --stop-instance   Stop instance instead of failing because it is in
                        'running' state.
  --reboot-if-needed    Reboot the new instance if needed.
  --delete-ami          Delete AMI at end of conversion.
  --do-not-require-stopped-instance
                        Allow instance conversion while instance is in
                        'running' state. (NOT RECOMMENDED)
  -d, --debug           Turn on debug traces.
  -v, --version         Display tool version.
  -r, --review-conversion-result
                        Display side-by-side conversion result. Note: REQUIRES
                        'VIM' EDITOR!
```

> At the end of a conversion, the tool can replay as many times as wished former conversion results specifying the original instance id: It will display again all execution steps and it can be useful to review again the conversion result (VIm Diff window) of a previous run. The `--delete-ami` option can also be added in a subsequent call to suppress the AMI and associated snapshots built by a previous tool execution.


## Resilience 

The tool is designed with maximum safety of stateful data and operations in mind: All conversion states are persisted in a DynamoDB table and, if the tool is interrupted or
encounters an error, it should be restartable where it went interrupted without special user action. In the unexpected (and unlikely) event of a major bug and bad outcome, 
please consult the DynamoDB line corresponding to your instance Id: This line contains JSON states of your original instance and other information (AMI, Interfaces...)
allowing to reconstruct the original instance by hand. **In such event, please also create a GitHub issue with a precise description of the encountered problem!**

## Contributing

If you'd like to contribute, please fork the repository and use a feature
branch. Pull requests are warmly welcome.

## Licensing

The code in this project is licensed under [MIT license](LICENSE).

