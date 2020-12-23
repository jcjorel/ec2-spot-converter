#!/bin/bash -e

VERSION=$1
APP_NAME=ec2-spot-converter
if [ -z "$VERSION" ] ; then
	echo "Usage: $0 <version>" ; exit 1
fi
versionned_file="${APP_NAME}-${VERSION}"
if [ -e "releases/${versionned_file}" ]; then
	echo "Version ${VERSION} already exists!" ; exit 1
fi
if [ -z "$(echo $VERSION | grep rc)" ] ; then
	echo ${VERSION} >VERSION.txt
fi
sed "s/::Version::/$VERSION/g" <src/${APP_NAME}.py | 
	sed "s/::ReleaseDate::/$(date)/g" > releases/${versionned_file}
chmod a+x releases/${versionned_file}
ln -sf ${versionned_file} releases/${APP_NAME}-latest
git add releases/${versionned_file} releases/${APP_NAME}-latest
git commit -m "Releasing ${APP_NAME} version $VERSION" releases/${versionned_file} releases/${APP_NAME}-latest VERSION.txt
git tag -a $VERSION -m "Releasing ${APP_NAME} version $VERSION"
git push
git push --tags
