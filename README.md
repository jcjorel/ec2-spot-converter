# ec2-spot-converter

This tool converts existing EC2 instances back and forth from on-demand and 'persistent' Spot billing models while preserving
instance attributes (Launch configuration, Tags..), network attributes (existing Private IP addresses, Elastic IP), storage (Volumes),
Elastic Inference accelerators and Elastic GPUs.

It also allows replacement of existing Spot instances with new "identical" ones to update the instance type and cpu options. 

Conversion time ranges from 2 to 5 minutes depending on the instance type.


# Getting started

**Prerequisistes:**
* Install the tool on an EC2 Linux instance located **in the same account and region than the instance to convert**,

```shell
curl https://raw.githubusercontent.com/jcjorel/ec2-spot-converter/master/ec2-spot-converter -o ec2-spot-converter
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

# Convert running On-Demand instance i-0b762953e069c7faa to Spot model
#   This instance has 11 attached volumes, 2 ENIs and 1 EIP.
$ ./ec2-spot-converter --stop-instance --review-conversion-result --instance-id i-0b762953e069c7faa
[STEP 1/19] Read DynamoDB state table...
  => SUCCESS. Record 'i-046a39e67ab406035' doesn't exist yet.
[STEP 2/19] Discover Instance state...
  => SUCCESS. Stopping i-046a39e67ab406035...
[STEP 3/19] Wait for 'stopped' Instance state...
Waiting for instance to stop...
...
Waiting for instance to stop...
  => SUCCESS. Instance in 'stopped' state.
[STEP 4/19] Get storage volume details...
  => SUCCESS. Successfully retrieved volume details for ['vol-03d65e82f9631468d', 'vol-0605690bc4c01e6b2', 'vol-02158f4f205812417', 'vol-03b5d5daf76f6c874', 'vol-09e8a24ed6ca74d55', 'vol-09b85179e4b24bc83', 'vol-023b93b772a974c4e', 'vol-06bec3a639f0ea1f9', 'vol-071b462a1c761cec7', 'vol-0c37d6cd7247a58d3', 'vol-0129b9e38c2c87b77'].
[STEP 5/19] Detach instance volumes with DeleteOnTermination=False...
Detaching volume vol-0605690bc4c01e6b2...
Detaching volume vol-02158f4f205812417...
Detaching volume vol-03b5d5daf76f6c874...
Detaching volume vol-09e8a24ed6ca74d55...
Detaching volume vol-09b85179e4b24bc83...
Detaching volume vol-023b93b772a974c4e...
Detaching volume vol-06bec3a639f0ea1f9...
Detaching volume vol-071b462a1c761cec7...
Detaching volume vol-0c37d6cd7247a58d3...
Detaching volume vol-0129b9e38c2c87b77...
  => SUCCESS. Detached volumes ['vol-0605690bc4c01e6b2', 'vol-02158f4f205812417', 'vol-03b5d5daf76f6c874', 'vol-09e8a24ed6ca74d55', 'vol-09b85179e4b24bc83', 'vol-023b93b772a974c4e', 'vol-06bec3a639f0ea1f9', 'vol-071b462a1c761cec7', 'vol-0c37d6cd7247a58d3', 'vol-0129b9e38c2c87b77'].
[STEP 6/19] Wait for volume detach status...
  => SUCCESS. All detached volumes are 'available : ['vol-0605690bc4c01e6b2', 'vol-02158f4f205812417', 'vol-03b5d5daf76f6c874', 'vol-09e8a24ed6ca74d55', 'vol-09b85179e4b24bc83', 'vol-023b93b772a974c4e', 'vol-06bec3a639f0ea1f9', 'vol-071b462a1c761cec7', 'vol-0c37d6cd7247a58d3', 'vol-0129b9e38c2c87b77'].
[STEP 7/19] Start AMI creation...
AMI Block device mapping: [{'DeviceName': '/dev/xvda', 'Ebs': {'DeleteOnTermination': True, 'VolumeSize': 8, 'VolumeType': 'gp2'}}]
  => SUCCESS. AMI image ec2-spot-converter-i-046a39e67ab406035/ami-0463d1d65c3d8ea02 started.
[STEP 8/19] Tag all resources (Instance, ENI(s), Volumes) with ec2-spot-converter job Id...
  => SUCCESS. Successfully tagged ['i-046a39e67ab406035', 'eni-0656574bbc1d37c5c', 'eni-0d1fc6467a66a1c21', 'vol-0605690bc4c01e6b2', 'vol-02158f4f205812417', 'vol-03b5d5daf76f6c874', 'vol-09e8a24ed6ca74d55', 'vol-09b85179e4b24bc83', 'vol-023b93b772a974c4e', 'vol-06bec3a639f0ea1f9', 'vol-071b462a1c761cec7', 'vol-0c37d6cd7247a58d3', 'vol-0129b9e38c2c87b77'].
[STEP 9/19] Prepare network interfaces for instance disconnection...
  => SUCCESS. Successfully prepared network interfaces ['eni-0656574bbc1d37c5c', 'eni-0d1fc6467a66a1c21'].
[STEP 10/19] Wait for AMI to be ready...
Waiting for image ami-0463d1d65c3d8ea02 to be available...
...
Waiting for image ami-0463d1d65c3d8ea02 to be available...
  => SUCCESS. AMI ami-0463d1d65c3d8ea02 is ready..
[STEP 11/19] Checkpoint the current exact state of the instance...
  => SUCCESS. Checkpointed instance state...
[STEP 12/19] Terminate instance...
  => SUCCESS. Successfully terminated instance i-046a39e67ab406035.
[STEP 13/19] Create new instance...
  => SUCCESS. Created new instance 'i-049e3d26fd61455da'.
[STEP 14/19] Wait new instance to come up...
  => SUCCESS. Instance i-049e3d26fd61455da is in 'running' state.
[STEP 15/19] Reattach volumes...
Attaching volume vol-0605690bc4c01e6b2 to i-049e3d26fd61455da with device name /dev/sdw...
Attaching volume vol-02158f4f205812417 to i-049e3d26fd61455da with device name /dev/sdx...
Attaching volume vol-03b5d5daf76f6c874 to i-049e3d26fd61455da with device name /dev/sdy...
Attaching volume vol-09e8a24ed6ca74d55 to i-049e3d26fd61455da with device name /dev/sdz...
Attaching volume vol-09b85179e4b24bc83 to i-049e3d26fd61455da with device name /dev/sdi...
Attaching volume vol-023b93b772a974c4e to i-049e3d26fd61455da with device name /dev/sdh...
Attaching volume vol-06bec3a639f0ea1f9 to i-049e3d26fd61455da with device name /dev/sdg...
Attaching volume vol-071b462a1c761cec7 to i-049e3d26fd61455da with device name /dev/sdf...
Attaching volume vol-0c37d6cd7247a58d3 to i-049e3d26fd61455da with device name /dev/sdv...
Attaching volume vol-0129b9e38c2c87b77 to i-049e3d26fd61455da with device name /dev/sdu...
 - /!\ WARNING /!\ Volumes attached after boot. YOUR INSTANCE MAY NEED A REBOOT!
  => SUCCESS. Successfully reattached volumes ['vol-0605690bc4c01e6b2', 'vol-02158f4f205812417', 'vol-03b5d5daf76f6c874', 'vol-09e8a24ed6ca74d55', 'vol-09b85179e4b24bc83', 'vol-023b93b772a974c4e', 'vol-06bec3a639f0ea1f9', 'vol-071b462a1c761cec7', 'vol-0c37d6cd7247a58d3', 'vol-0129b9e38c2c87b77']...
[STEP 16/19] Configure network interfaces...
Setting 'DeleteOnTermination=True' for interface eni-0d1fc6467a66a1c21...
Setting 'DeleteOnTermination=True' for interface eni-0656574bbc1d37c5c...
  => SUCCESS. Successfully configured network interfaces ['eni-0656574bbc1d37c5c', 'eni-0d1fc6467a66a1c21'].
[STEP 17/19] Manage Elastic IP...
  => SUCCESS. Reassociated EIPs '['34.247.111.29']'...
[STEP 18/19] Untag resources...
  => SUCCESS. Successfully untagged ['eni-0656574bbc1d37c5c', 'eni-0d1fc6467a66a1c21'].
[STEP 19/19] Deregister image...
  => SUCCESS. Successfully deregistered AMI 'ami-0463d1d65c3d8ea02'.
Conversion successful! New instance id: i-049e3d26fd61455da, ElapsedTime: 187 seconds
 - /!\ WARNING /!\ Volumes attached after boot. YOUR INSTANCE MAY NEED A REBOOT!
```
As the option `--review-conversion-result` is specified, at conversion end, a **VIm Diff** window pops-up and allow interactive review of the differences between
the original now terminated instance and the newly created one: You should see differences only related to dates, AssociationIds, AMI (and storage(s) with 'DeleteOnTermination=True').

![Result review window](review-result.png)

### Convert a Spot instance to On-Demand model

The operation is similar than the conversion to Spot model with the difference that the option `--target-billing-model` must be set to `on-demand` explicitly.

> Note: `ec2-spot-converter` can also convert back an instance currently in **Spot interrupted** state to On-Demand model.

### Convert a Spot instance to Spot model to change the instance type and/or CPU Options

`ec2-spot-converter`tool can be used to replace a Spot instance by another one just changing the instance type. This operation is not 
possible currently "in-place" through an AWS EC2 API so the tool will terminate and replace an instance preserving all attributes but
updating the instance type (or CPU options) during the process.

Specify options `--instance-type` and/or `--cpu-options`on an existing Spot instance to start conversion. 

# Command line usage

```shell
ec2-spot-converter -h
usage: ec2-spot-converter [-h] --instance-id INSTANCE_ID
                          [--target-billing-model {spot,on-demand}]
                          [--target-instance-type TARGET_INSTANCE_TYPE]
                          [--cpu-options CPU_OPTIONS]
                          [--max-spot-price MAX_SPOT_PRICE]
                          [--dynamodb-tablename DYNAMODB_TABLENAME]
                          [--generate-dynamodb-table] [--stop-instance]
                          [--debug] [--review-conversion-result]

EC2 Spot converter

optional arguments:
  -h, --help            show this help message and exit
  --instance-id INSTANCE_ID
                        The id of the EC2 instance to convert.
  --target-billing-model {spot,on-demand}
                        The expected billing model after conversion. Default:
                        'spot'
  --target-instance-type TARGET_INSTANCE_TYPE
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
  --stop-instance       Stop instance instead of failing because it is in
                        'running' state.
  --debug               Turn on debug traces.
  --review-conversion-result
                        Display side-by-side conversion result. Note:
                        REQUIRES 'VIM' EDITOR!
```

## Contributing

If you'd like to contribute, please fork the repository and use a feature
branch. Pull requests are warmly welcome.

## Licensing

The code in this project is licensed under [MIT license](LICENSE).

